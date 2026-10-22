"""Phase 1 demo on the pilot floor (F6).

Reads the architect's DWG, finds rooms, runs the read-only checks, regenerates the
small-power sockets of the repeated apartment type from a BS 7671 rule set, draws a
demo circuit, mirrors a reviewed unit onto its twin, validates the overlay DXFs,
renders review images and plots the small power sheet to PDF with the overlay.
Every step is timed.

    .venv/bin/python -I scripts/demo_f6.py \
        --drawing "work/input/F6/<pilot floor>.dwg" --workdir work
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from nfe_electrical import checks, render
from nfe_electrical.compare import SOCKET_TYPES, compare_devices, comparison_markdown, mirror_consistency
from nfe_electrical.floor import Floor
from nfe_electrical.overlay import Overlay
from nfe_electrical.placement import circuit_run, mirror_unit
from nfe_electrical.rules import apply_rules, load_rules

HERE = Path(__file__).resolve().parent.parent


class Timer:
    def __init__(self):
        self.steps: dict[str, float] = {}

    def __call__(self, name):
        timer = self

        class _Step:
            def __enter__(self):
                self.t0 = time.perf_counter()
                print(f"-> {name} ...", flush=True)

            def __exit__(self, *exc):
                timer.steps[name] = round(time.perf_counter() - self.t0, 2)
                print(f"   {name}: {timer.steps[name]} s", flush=True)
        return _Step()


def main() -> None:
    logging.getLogger("ezdxf").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--drawing", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--floor-id", default="F6")
    ap.add_argument("--rules", default=str(HERE / "rules" / "bs7671" / "apartment_type_a_small_power.yaml"))
    ap.add_argument("--standard", default="BS 7671", help="the job's governing standard")
    args = ap.parse_args()
    t = Timer()
    t_all = time.perf_counter()

    with t("1 read + normalise (copy, dwg2dxf, ezdxf load, sheets, symbols, units, base)"):
        f = Floor.open(args.drawing, args.workdir, floor_id=args.floor_id, standard=args.standard)
        reports = f.write_reports()
    out = f.out_dir
    renders = f.workspace.floor_dir(f.floor_id, "renders")

    with t("2 find rooms"):
        rs = f.rooms
        rs.write(out / "rooms.json")
    with t("2b render room map"):
        render.render_rooms(f, out / "rooms.png")

    with t("3 read-only checks (title blocks, legends, device counts)"):
        res = checks.run_all_checks(f)
        (out / "checks.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
        (out / "checks.md").write_text(checks.checks_markdown(res), encoding="utf-8")
        checks.write_device_counts_csv(res["device_counts"], out / "device_counts.csv")

    rules = load_rules(args.rules, f.catalogue)
    m = f.mirror
    units = list(m.sides) if m else []
    with t("4 regenerate sockets from rules (both units)"):
        ov = Overlay.open(f, f"{f.floor_id}_typeA_small_power", fresh=True)
        rep = apply_rules(f, ov, rules, units=units)

    with t("5 demo circuit run (unit W lounge sockets from the W consumer unit)"):
        lounge = f.rooms.get(f"Lounge [{units[0]}]") if units else None
        cct = None
        if lounge is not None:
            devs = [d["handle"] for d in ov.devices() if d.get("room") == lounge.room_id and d["type"] == "socket_twin_13a"]
            cus = [s for s in f.symbols_on_plan(types={"consumer_unit"}) if s.room_id and f.rooms.get(s.room_id).unit == lounge.unit]
            if devs and cus:
                cu = cus[0]
                cct = circuit_run(f, ov, devs, "C1", start=cu.handle, ring=True, kind="power",
                                  db=cu.attribs.get("CU") or "CU")

    with t("6 compare with the engineer's layout"):
        ids = [r.room_id for r in f.rooms.rooms if r.unit in units]
        cmp = compare_devices(f, ov, ids, SOCKET_TYPES)
        mir = mirror_consistency(f)

    with t("7 copy reviewed unit W onto unit E (mirror)"):
        ovm = Overlay.open(f, f"{f.floor_id}_typeA_W_mirrored", fresh=True)
        apply_rules(f, ovm, rules, units=[units[0]])
        mres = mirror_unit(f, ovm, units[0], units[1])
        cmp_m = compare_devices(f, ovm, [r.room_id for r in f.rooms.rooms if r.unit == units[1]], SOCKET_TYPES)

    with t("8 validate overlays (ezdxf strict re-read + LibreDWG round trip)"):
        val = {"main": ov.validate(libredwg=True), "mirrored": ovm.validate(libredwg=True)}
        (out / "validation.json").write_text(json.dumps(val, indent=1, default=str), encoding="utf-8")

    with t("9 review renders (PNG + PDF pack)"):
        files = []
        files.append(render.render_crop(f, renders / f"{f.floor_id}_plan_overlay.png", overlay=ov,
                                        title=f"{f.floor_id}: regenerated sockets (magenta) over the engineer's (blue)"))
        for u in units:
            ids_u = [r.room_id for r in f.rooms.rooms if r.unit == u]
            pairs = [p for r in cmp["rooms"] if r["unit"] == u for p in r["pairs"]]
            files.append(render.render_crop(f, renders / f"{f.floor_id}_unit_{u}_compare.png",
                                            window=render.room_window(f, ids_u), overlay=ov, pairs=pairs,
                                            title=f"{f.floor_id} unit {u}: rules (magenta) vs engineer (blue); "
                                                  "lines pair them up"))
        ids_e = [r.room_id for r in f.rooms.rooms if r.unit == units[1]]
        files.append(render.render_crop(f, renders / f"{f.floor_id}_unit_{units[1]}_mirrored_compare.png",
                                        window=render.room_window(f, ids_e), overlay=ovm,
                                        pairs=[p for r in cmp_m["rooms"] for p in r["pairs"]],
                                        title=f"{f.floor_id} unit {units[1]}: unit {units[0]} mirrored (magenta) vs engineer (blue)"))
        crops = []
        for name in (f"Bedroom [{units[0]}]", f"Lounge [{units[0]}]", f"Kitchen [{units[0]}]", f"Cinema [{units[0]}]"):
            try:
                r = f.rooms.get(name)
            except KeyError:
                continue
            crops.append({"window": render.room_window(f, [r.room_id], 0.8), "room_ids": [r.room_id],
                          "title": f"{r.room_id} {r.name} [{r.unit}]",
                          "pairs": [p for x in cmp["rooms"] if x["room"] == r.room_id for p in x["pairs"]]})
        pages = [{"title": f"{f.floor_id} plan: regenerated sockets over the engineer's"}] + [
            {"window": render.room_window(f, [r.room_id for r in f.rooms.rooms if r.unit == u]),
             "title": f"Unit {u}", "pairs": [p for r in cmp["rooms"] if r["unit"] == u for p in r["pairs"]]}
            for u in units] + crops
        files.append(render.review_pack(f, renders / f"{f.floor_id}_review_pack.pdf", pages, overlay=ov))

    with t("10 sheet PDFs (small power sheet at A1: as issued, and for review)"):
        sheets = f.workspace.floor_dir(f.floor_id, "sheets")
        power_sheet = next((s.layout for s in f.sheets if s.discipline == "small_power"), f.plan_sheet.layout)
        sheet_pdfs = [render.plot_sheet(f, sheets / f"{f.floor_id}_small_power_{mode}.pdf", layout=power_sheet,
                                        overlay=ov, mode=mode) for mode in ("issue", "review")]

    total = round(time.perf_counter() - t_all, 2)
    (out / "comparison.json").write_text(json.dumps({"rules_both_units": cmp, "mirror_W_to_E": cmp_m,
                                                     "engineer_mirror_consistency": mir}, indent=1, default=str),
                                         encoding="utf-8")
    md = [f"# {f.floor_id} demo results", "",
          f"Governing standard: {f.standard}. Rule set: `{Path(args.rules).name}` ({rules.name}, "
          f"{rules.version}).", "",
          comparison_markdown(cmp, mir, "Rules applied to both units independently"),
          comparison_markdown(cmp_m, None, f"Unit {units[0]} generated from rules, then mirrored onto unit {units[1]}")]
    (out / "comparison.md").write_text("\n".join(md), encoding="utf-8")
    timings = {"steps_s": t.steps, "total_s": total, "floor_internal": f.timings}
    (out / "timings.json").write_text(json.dumps(timings, indent=1), encoding="utf-8")
    summary = {
        "floor": f.floor_id, "reports": reports, "overlay": str(ov.path), "overlay_mirrored": str(ovm.path),
        "rooms": rs.summary(), "check_issues": res["summary"], "devices_placed": rep["devices_placed"],
        "rule_warnings": rep["warnings"], "circuit": {k: cct[k] for k in ("circuit", "length_m", "wall_crossings")}
        if cct else None, "comparison": cmp["totals"], "mirror_comparison": cmp_m["totals"],
        "mirror_skipped": len(mres["skipped"]), "validation": {
            k: {"ezdxf_audit_errors": v["ezdxf"]["audit_errors"], "counts_match": v["ezdxf"]["counts_match"],
                "only_nfe_layers": v["only_nfe_layers"],
                "libredwg_inserts_match": v.get("libredwg", {}).get("inserts_match"),
                "libredwg_attribs_match": v.get("libredwg", {}).get("attribs_match")} for k, v in val.items()},
        "renders": files, "sheets": sheet_pdfs, "timings": timings}
    (out / "demo_summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("rooms", "check_issues", "devices_placed", "circuit", "comparison",
                                              "mirror_comparison", "validation")}, indent=1, default=str))
    print(f"total {total} s; outputs in {out}")


if __name__ == "__main__":
    main()
