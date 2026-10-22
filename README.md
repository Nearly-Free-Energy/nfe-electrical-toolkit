# NFE electrical toolkit (phase 1 pilot)

An open-source Python toolkit, with a local MCP server, that reads an architect's DWG floor plan, checks NFE's existing electrical drawing, finds the rooms, and writes new electrical work to a **separate overlay DXF** on NFE layers. It then plots the sheets to PDF with that overlay. DXF plus PDF is NFE's standard deliverable. Claude (or any MCP client) names rooms, device types and rules; the toolkit computes every coordinate. Nothing ever writes to the architect's drawing.

Phase 1 was built and demonstrated on one floor of a real NFE project (the pilot floor, F6). The pilot report, with results, timings and open decisions, is kept in NFE's internal documentation rather than in this repository.

| Step | Module | What it does |
|---|---|---|
| Read and normalise | `convert.py`, `floor.py`, `sheets.py`, `units.py`, `basepick.py`, `symbols.py` | Converts DWG with LibreDWG `dwg2dxf`; loads with ezdxf; rebuilds sheet views from viewport geometry (LibreDWG zeroes the on/off flags) and takes as the plan the largest viewport that shows the drawing; detects the real units from geometry, but never overrules the declared unit on fewer than three signals; picks the plotted base among PDF-import copies; indexes every electrical symbol by block name and **drawn** centre |
| Rooms | `rooms.py`, `apartments.py` | Fills outward from room labels to the walls; treats dashed beam lines as zone boundaries; reports failed labels; finds mirrored apartment units (at least three mirrored pairs of rooms that are not ducts, lifts or stairs) |
| Read-only checks | `checks.py` | Title blocks and drawing numbers; sheets (empty, stale copies of the plan viewport, entities off the paper); legends against the symbols used on each sheet; device counts per room |
| Placement | `placement.py`, `fixtures.py`, `overlay.py` | `place_device(room, type, rule)`, a circuit-run tool and a mirror-unit tool; write only to the overlay, correct base-point offsets, learn wall offsets and orientation from the engineer's existing symbols |
| Rules | `rules.py`, `rules/<standard>/*.yaml` | Applies a YAML rule set written for the job's governing standard (BS 7671 by default) |
| Compare | `compare.py` | Counts and positions against the engineer's layout |
| Review renders and sheets | `render.py` | PNG/PDF crops of the overlay over the grey base, and true-size sheet PDFs (title block, legend, plan) with the overlay |
| MCP server | `mcp_server.py` | All of the above as 17 tools over stdio |

## Install

You need Python 3.11 or newer and the LibreDWG command-line tools (`dwg2dxf`; `dxf2dwg` and `dwgread` for the optional round-trip check). The toolkit runs on macOS, Windows and Linux: CI runs the tests on all three with Python 3.11 and 3.13, and the pilot used 3.14.7 on macOS.

- macOS: `brew install libredwg`
- Linux: your distribution's `libredwg` package, or build from <https://www.gnu.org/software/libredwg/>
- Windows: LibreDWG's release zip, unzipped into `%LOCALAPPDATA%\Programs\LibreDWG` (see [Windows](#windows))

The toolkit looks for the LibreDWG programs on `PATH`, then in those usual places. If you keep them anywhere else, set `NFE_DWG2DXF`, `NFE_DXF2DWG` and `NFE_DWGREAD` to their full paths.

Then clone the repository and set up a virtual environment in it (on Windows, follow [Windows](#windows) instead):

```bash
git clone https://github.com/Nearly-Free-Energy/nfe-electrical-toolkit.git
```

```bash
cd nfe-electrical-toolkit
```

```bash
python3 -m venv .venv
```

```bash
.venv/bin/python -I -m pip install -e ".[test]"
```

Always run the toolkit's Python with `-I` (isolated mode), so nothing in the current directory or in a drawing folder can be imported by accident.

## Windows

The toolkit runs on Windows 10 and 11. Run these commands in PowerShell.

1. Install Python 3.11 or newer from <https://www.python.org/downloads/windows/> (the installer includes the `py` launcher), and [Git for Windows](https://git-scm.com/download/win).
2. Install LibreDWG from its Windows release zip. Unzip it into `%LOCALAPPDATA%\Programs\LibreDWG`, so that `dwg2dxf.exe` sits directly in that folder. The toolkit looks there after `PATH`, so nothing else needs setting, and no administrator rights are needed. MSYS2 has no LibreDWG package.

```powershell
Invoke-WebRequest https://github.com/LibreDWG/libredwg/releases/download/0.14/libredwg-0.14-win64.zip -OutFile "$env:TEMP\libredwg-0.14-win64.zip"
```

```powershell
(Get-FileHash "$env:TEMP\libredwg-0.14-win64.zip").Hash
```

That should print `1AD7E15344D20B3426C3435B078D82FB84B35062815946B2CCA9C5FC9810FEA8`, the SHA-256 that GitHub lists for the [LibreDWG 0.14 release](https://github.com/LibreDWG/libredwg/releases/tag/0.14).

```powershell
Expand-Archive "$env:TEMP\libredwg-0.14-win64.zip" -DestinationPath "$env:LOCALAPPDATA\Programs\LibreDWG"
```

```powershell
& "$env:LOCALAPPDATA\Programs\LibreDWG\dwg2dxf.exe" --version
```

3. Clone the repository and set up a virtual environment in it:

```powershell
git clone https://github.com/Nearly-Free-Energy/nfe-electrical-toolkit.git
```

```powershell
cd nfe-electrical-toolkit
```

```powershell
py -m venv .venv
```

```powershell
.venv\Scripts\python.exe -I -m pip install -e ".[test]"
```

From then on, use `.venv\Scripts\python.exe` wherever this README says `.venv/bin/python`, for example:

```powershell
.venv\Scripts\python.exe -I -m nfe_electrical.cli analyse "C:\path\to\F6.dwg" --workdir work --floor-id F6
```

**Claude Desktop** reads `%APPDATA%\Claude\claude_desktop_config.json` (Settings > Developer > Edit Config opens it). Add the server there and restart the app. JSON needs every backslash doubled:

```json
{
  "mcpServers": {
    "nfe-electrical": {
      "command": "C:\\path\\to\\nfe-electrical-toolkit\\.venv\\Scripts\\python.exe",
      "args": ["-I", "-m", "nfe_electrical.mcp_server", "--workdir", "C:\\path\\to\\nfe-electrical-work"]
    }
  }
}
```

**Claude Code**:

```powershell
claude mcp add nfe-electrical --scope user -- C:\path\to\nfe-electrical-toolkit\.venv\Scripts\python.exe -I -m nfe_electrical.mcp_server --workdir C:\path\to\nfe-electrical-work
```

What is different on Windows:

- Windows does not let one program replace a file that another program has open. If an overlay DXF is open in a CAD program or viewer when the toolkit saves it, the toolkit stops with a message and keeps the new version next to it as `<name>.tmp.dxf`. Close the file and run the step again: it starts from the overlay as last saved, so nothing is added twice.
- The input copies in `<workdir>\input\` are read-only, so `Remove-Item -Recurse` without `-Force`, and Python's `shutil.rmtree`, refuse to delete a work folder. Delete it in Explorer, with `Remove-Item -Recurse -Force <workdir>`, or from Python with `nfe_electrical.workspace.remove_workdir("<workdir>")`.
- Reports are written as UTF-8 whatever the system code page. `device_counts.csv` starts with a byte-order mark, so Excel shows room names with accents or symbols correctly.

## Use it from the command line

```bash
.venv/bin/python -I -m nfe_electrical.cli analyse "/path/to/F6.dwg" --workdir work --floor-id F6
```

`analyse` copies the drawing into `work/input/F6/` (read-only), converts it, finds rooms and runs every check. It writes `floor.json`, `symbols.json`, `block_audit.json`, `rooms.json`, `rooms.png`, `checks.json`, `checks.md` and `device_counts.csv` to `work/F6/`.

Other commands all take the same drawing, `--workdir`, `--floor-id` and `--standard` (the job's governing standard; default BS 7671):

| Command | Purpose |
|---|---|
| `rooms` | Room finder only |
| `check` | Title block, legend and device-count checks |
| `apply-rules --rules rules/bs7671/apartment_type_a_small_power.yaml --overlay NAME [--units W E \| --rooms R07 R08] [--fresh] [--dry-run]` | Place devices from a rule set, listing every device's position per room. `--dry-run` writes nothing and gives exactly the positions a write would. A write is all or nothing: if a rule fails, the overlay is left as it was |
| `mirror --overlay NAME --from W --to E` | Copy one apartment's overlay devices onto its mirror image |
| `compare --overlay NAME [--units W]` | Compare overlay sockets with the engineer's |
| `render --overlay NAME [--unit W \| --room R07 \| --rooms R07 R08] [--rules FILE] [--out file.png\|.pdf]` | Review image. With `--rules`, draws what the rule set would add to the overlay (on the unit, the rooms, or by default the rule set's own scope) and writes nothing: a proposal to show before writing |
| `plot-sheet --overlay NAME [--layout "SMALLPOWER LAYOUT"] [--mode issue\|review]` | The sheet at true size (e.g. A1) as PDF. `issue` mode shows the overlay in place of the existing symbols it covers; `review` mode draws it in magenta on top |
| `validate --overlay NAME [--no-libredwg]` | Re-read the overlay with ezdxf (and LibreDWG) |
| `serve --workdir work` | Run the MCP server |

## Run the F6 demo

```bash
.venv/bin/python -I scripts/demo_f6.py --drawing "work/input/F6/<pilot floor>.dwg" --workdir work
```

It takes about 55 seconds and writes, in `work/F6/`:

- `overlays/F6_typeA_small_power.dxf`: the regenerated sockets for both apartments plus a demo ring circuit (the main deliverable)
- `overlays/F6_typeA_W_mirrored.dxf`: unit W from the rules, mirrored onto unit E
- `sheets/`: `F6_small_power_issue.pdf` (the small power sheet at A1, with the regenerated sockets in place of the engineer's) and `F6_small_power_review.pdf` (the overlay in magenta over the existing sheet)
- `renders/`: `F6_plan_overlay.png`, `F6_unit_W_compare.png`, `F6_unit_E_compare.png`, `F6_unit_E_mirrored_compare.png` and the 7-page `F6_review_pack.pdf`
- `rooms.json` and `rooms.png`; `checks.json`, `checks.md` and `device_counts.csv`
- `comparison.md` and `comparison.json`; `validation.json`, `timings.json` and `demo_summary.json`

The `work/` folder holds client drawings and data derived from them. `.gitignore` keeps it, and any DWG, DXF, render or sheet anywhere else, out of the repository. The tests build their own synthetic drawing.

## Use it from Claude (MCP)

The server runs locally over stdio, so it needs no network access, and the Team seats' Claude Desktop or Claude Code can use it directly.

**Claude Code** (run from any folder; `--scope user` makes it available in every project):

```bash
claude mcp add nfe-electrical --scope user -- /path/to/nfe-electrical-toolkit/.venv/bin/python -I -m nfe_electrical.mcp_server --workdir /path/to/nfe-electrical-work
```

Or commit a `.mcp.json` like this to a project:

```json
{
  "mcpServers": {
    "nfe-electrical": {
      "command": "/path/to/nfe-electrical-toolkit/.venv/bin/python",
      "args": ["-I", "-m", "nfe_electrical.mcp_server", "--workdir", "/path/to/nfe-electrical-work"]
    }
  }
}
```

**Claude Desktop**: add the same `mcpServers` entry to `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`; Windows: `%APPDATA%\Claude\claude_desktop_config.json`, with the paths shown under [Windows](#windows)) and restart the app.

Tools:

| Tool | Writes? | Purpose |
|---|---|---|
| `open_floor` | work folder only | Import, convert and normalise a drawing; returns a `floor_id` |
| `floor_report`, `list_symbols`, `list_device_types` | no | Units, sheets, base copies, block offsets, symbols, placeable device types |
| `find_rooms`, `get_room` | `rooms.json`, `rooms.png` | Rooms, areas, mirrored units, walls and what they back onto, recognised furniture |
| `run_checks` | check reports | Title blocks, sheets, legends, device counts |
| `place_device` | overlay (if `dry_run=false`) | One device type in one room by rule, e.g. `pair:bed_head`, `wall:media_wall:3`, `perimeter:2` |
| `apply_rules` | overlay (if `dry_run=false`) | A YAML rules file over units or rooms; all or nothing |
| `mirror_unit_layout` | overlay (if `dry_run=false`) | Copy a reviewed unit onto its mirror image |
| `add_circuit_run` | overlay (if `dry_run=false`) | Orthogonal run from a board through devices; tags the circuit on the devices |
| `overlay_status`, `remove_overlay_items`, `validate_overlay` | overlay | Inspect, prune and check the overlay |
| `compare_with_existing` | no | Counts and positions against the engineer's symbols |
| `render_review` | a PNG/PDF | Returns the review image to Claude as well. With `rules_path`, previews a rule set without writing it |
| `plot_sheet` | a sheet PDF | A paper-space sheet at true size with the overlay (`issue` or `review` mode) |

All writing tools default to `dry_run=true`: Claude proposes, the engineer looks at the render (`render_review` with `rules_path` draws a rule set's proposal), and only then is the overlay written. Example request: *"Open work/input/F6/…dwg as F6, find the rooms, and propose bedside sockets for both bedrooms with pair:bed_head. Show me a render before writing anything."*

## The overlay DXF

- AutoCAD 2018 DXF (AC1032), in the base drawing's real units (`$INSUNITS` set to the detected unit) and coordinates. Attach it to the untouched architect's DWG as an xref at 0,0,0, scale 1, or open the two side by side.
- Model-space entities only on `NFE-E-*` layers (`NFE-E-POWER`, `NFE-E-POWER-CCT`, `NFE-E-ANNO` and so on, defined in `src/nfe_electrical/data/catalogue.yaml`).
- Symbols are copies of NFE's own blocks named `NFE_<block>` (e.g. `NFE_SOCKET`). They keep their attributes and gain invisible `NFE_TYPE`, `NFE_ROOM`, `NFE_CIRCUIT`, `NFE_DB`, `NFE_MOUNT_MM` and `NFE_RULE` attributes. Their geometry sits on layer 0, so the NFE layer controls colour, and is re-centred on its drawn centre (the library clean-up the research recommends). `Overlay.open(..., recentre_blocks=False)` keeps the original far-away base points, and placement still lands within a millimetre; the tests check both.
- Each item carries `NFE_TOOLKIT` XDATA (type, room, rule, toolkit version, time). The header carries the source drawing's name and SHA-256 hash and the job's governing standard.

## Standards and rule sets

Each job records its governing standard (`--standard`, or `standard` in `open_floor`). BS 7671 is the default; IEC 60364 and US EAS 811-1:2014 are recognised for jobs that need them. Rule sets live in one folder per standard ([`rules/README.md`](rules/README.md)), and each declares the standard it was written for. The toolkit refuses to apply a rule set to a job under a different standard. None of these standards sets socket counts or positions: the rule sets are NFE's own design rules, and the engineers own them.

See [`rules/bs7671/apartment_type_a_small_power.yaml`](rules/bs7671/apartment_type_a_small_power.yaml). In brief:

```yaml
name: BS 7671 - apartment type A - small power
standard: BS 7671
defaults: {door_clearance: 0.3, corner_clearance: 0.3, min_spacing: 0.8, avoid_facing: [balcony]}
rooms:
  bedroom:
    - {type: socket_twin_usb, count: 2, place: pair, wall: bed_head, spacing: 2.4}
  lounge:
    - {type: socket_twin_13a, count: 3, place: wall, wall: [media_wall, facing_lounge, longest]}
```

The strategies are `perimeter`, `wall`, `pair`, `corners`, `beside_door`, `centre` and `at`. The wall choices are `longest`, `shortest`, `long_sides`, `bed_head`, `media_wall`, `facing_<room type>`, `opposite_door` and the four compass directions. `count` can be a number or `{per_m2: 8, min: 2, max: 6}`. Device types and their source blocks are in `src/nfe_electrical/data/catalogue.yaml`; room vocabulary is in `room_types.yaml`. Engineers own all three files.

## Safety and data

- Received drawings are untrusted data. They are copied into a new, empty `input/<floor>/` folder and made read-only, and the originals are never opened for writing. `dwg2dxf` runs with a timeout from the cache folder, and Python runs with `-I`. The LibreDWG programs are never taken from the current folder, although Windows normally looks there first.
- The toolkit writes only inside its work folder and has no network code. When Claude uses the MCP server, tool results (room names, title-block text, renders) become conversation content and go to Anthropic under the Team plan's terms. Phone numbers are masked in check outputs.
- Text in a drawing is reported as data. The server instructions tell the model never to treat it as an instruction.

## Tests

```bash
.venv/bin/python -I -m pytest -q
```

`tests/test_synthetic.py` builds a small drawing on the fly: a walled room, a sheet with a viewport, and a socket block like NFE's existing ones, whose graphics sit 950 m from its base point. It checks units, rooms, wall-facing placement, base-point correction, dry runs (that they place exactly as a write would), all-or-nothing rule sets, rule-set previews from the CLI and MCP server, circuits, sheet plotting, the title block's stated scale against the plot, existing symbols in renders, the standard check and dashed-line detection. It also covers what differs on Windows: UTF-8 reports, an overlay held open by another program, read-only input copies, finding LibreDWG, and (when LibreDWG is installed) reading a DWG made from the synthetic drawing. `tests/test_f6_integration.py` runs on the real F6 drawing when it is present in `work/input/F6/`, or when `NFE_F6_DWG` points to it.

CI ([`.github/workflows/tests.yml`](.github/workflows/tests.yml)) runs pyflakes and the tests on Windows, macOS and Linux with Python 3.11 and 3.13. A separate Windows job installs LibreDWG's release zip as described under [Windows](#windows) and sets `NFE_REQUIRE_LIBREDWG=1`, so the DWG test must run there rather than skip.

## Licences

Dependencies: ezdxf (MIT), shapely (BSD-3-Clause), mcp (MIT), numpy and scipy (BSD-3-Clause), matplotlib (matplotlib licence, BSD-style), PyYAML (MIT), Pillow (MIT-CMU), contourpy (BSD-3-Clause). LibreDWG (GPL-3.0) runs only as a separate program and is never linked. No ODA software is used. The toolkit itself is Copyright 2026 Nearly Free Energy, licensed under the Apache License 2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)).
