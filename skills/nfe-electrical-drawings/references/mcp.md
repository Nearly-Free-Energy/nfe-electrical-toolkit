# The MCP server (optional) and the Python API

The CLI covers the whole workflow, so the MCP server is optional. It helps when an engineer
works through many steps in one session:

- it keeps floors open between calls, where the CLI re-reads the drawing for every command;
- it has tools the CLI lacks: `place_device`, `add_circuit_run`, `remove_overlay_items` and
  `overlay_status`;
- `render_review` returns the image straight into the conversation.

It runs locally over stdio from the toolkit's own venv and has no network code. Tool results
(room names, title-block text, renders) become conversation content, like anything else Claude
reads.

## Set it up

Register it once per computer, for all projects. Paths must be absolute, and JSON doesn't
expand `~` or `$HOME`. On Windows, use `.venv\Scripts\python.exe`.

**With Claude Code installed** (the `claude` command), add it at user scope:

```bash
claude mcp add nfe-electrical --scope user -- "$HOME/nfe-electrical-toolkit/.venv/bin/python" -I -m nfe_electrical.mcp_server --workdir "$HOME/nfe-electrical-work"
```

`setup.py` prints this exact line, with the engineer's own paths, at the end of a successful
run. It is stored in `~/.claude.json`, which both Claude Code and the desktop app's local
Code-tab sessions read.

**With only the desktop app** (no `claude` command), add the entry to
`claude_desktop_config.json` instead. Open it from the app's Settings > Developer > Edit
Config; it lives in `~/Library/Application Support/Claude/` on macOS and `%APPDATA%\Claude\`
on Windows. Local Code-tab sessions load it as well as the Chat tab. Restart the app
afterwards.

```json
{
  "mcpServers": {
    "nfe-electrical": {
      "command": "/Users/NAME/nfe-electrical-toolkit/.venv/bin/python",
      "args": ["-I", "-m", "nfe_electrical.mcp_server", "--workdir", "/Users/NAME/nfe-electrical-work"]
    }
  }
}
```

The same entry in a project folder's `.mcp.json` limits the server to that folder. Start a new
session afterwards. `/mcp` in a session (or `claude mcp list`) should show `nfe-electrical` as
connected. The server starts in about 2 seconds, well inside Claude Code's 30-second MCP
startup limit (`MCP_TIMEOUT`).

The work folder is fixed when the server starts (`--workdir`, else `$NFE_WORKDIR`, else
`~/nfe-electrical-work`). Floor ids therefore have to be unique across jobs, as in `ABC-F6`.
After `setup.py` updates the toolkit, restart the server (start a new session, or reconnect it
in `/mcp`) so that it loads the new code.

## Tools

| Tool | Writes? | Purpose |
|---|---|---|
| `open_floor(drawing_path, floor_id, standard)` | work folder only | Import, convert and normalise a drawing; pass the governing standard (default BS 7671) |
| `floor_report`, `list_symbols`, `list_device_types` | no | Units, sheets, base copies, block offsets, symbols, placeable device types |
| `find_rooms`, `get_room` | `rooms.json`, `rooms.png` | Rooms, areas, mirrored units, walls and what they back onto, recognised furniture |
| `run_checks` | check reports | Title blocks, legends, device counts |
| `place_device` | overlay, if `dry_run=false` | One device type in one room by rule, e.g. `pair:bed_head`, `wall:media_wall:3`, `perimeter:2` |
| `apply_rules` | overlay, if `dry_run=false` | A YAML rules file over units or rooms |
| `mirror_unit_layout` | overlay, if `dry_run=false` | Copy a reviewed unit onto its mirror image |
| `add_circuit_run` | overlay, if `dry_run=false` | Orthogonal run from a board through devices (schematic only) |
| `overlay_status`, `remove_overlay_items`, `validate_overlay` | overlay | Inspect, prune and check the overlay |
| `compare_with_existing` | no | Counts and positions against the engineer's symbols |
| `render_review` | a PNG or PDF | Returns the review image as well |
| `plot_sheet` | a sheet PDF | A paper-space sheet at true size with the overlay (`issue` or `review`) |

## The same rules, with MCP

- Every writing tool defaults to `dry_run=true`. As with the CLI, a dry run leaves nothing to
  render. Its positions also ignore devices proposed by earlier rules in the same call, so they
  can overlap. So write proposals into a new overlay name each time (`proposal-1`,
  `proposal-2`, and so on), and show them with `render_review(overlay="proposal-1")`.
- The server holds overlays open in memory. Don't copy or edit overlay files on disk while it
  runs, because its next save would overwrite them. After the engineer approves, repeat the
  approved calls with `overlay="small_power"` and `dry_run=false`. Then use `overlay_status` and
  `render_review` to confirm that the result matches what was approved.
- Then `validate_overlay` and `plot_sheet(mode="issue")`, and deliver DXF + PDF.

## Why not `uvx --from git+https://github.com/...`?

A one-line `uvx --from git+https://github.com/Nearly-Free-Energy/nfe-electrical-toolkit nfe-elec-mcp`
looks simpler, but NFE doesn't use it for now:

- uvx installs a built wheel, not the repository. The rule sets in the repository's `rules/`
  folder aren't in that wheel (checked: it has no `rules/` files), so `apply_rules` has no rule
  sets to point at.
- Without a pin it runs whatever is on `main`, unreviewed. A pinned tag (`...@v0.1.0`) is
  looked up on GitHub at every start.
- uvx ignores lock files and resolves dependencies afresh on each machine, so engineers' installs
  drift apart over time.
- The first start downloads about 65 MB (6–7 minutes on a 200 KB/s line) before the server
  answers, far beyond the 30-second MCP startup limit.
- uv would be one more tool to install on every machine, and the Bash CLI needs the clone and
  venv anyway. The server in that venv is always the same version as the CLI.

## Without the MCP server: the Python API

For a single device, a circuit run or removing items when the server isn't set up, call the
library from the venv's Python. Use the same preamble variables as the CLI, and the same
proposal overlay and approval steps:

```bash
"$NFE_PY" -I - "$DWG" "$NFE_WORK" "$FLOOR" "$STD" <<'PY'
import json, sys
from nfe_electrical.floor import Floor
from nfe_electrical.overlay import Overlay
from nfe_electrical.placement import place_device, circuit_run
dwg, work, floor_id, std = sys.argv[1:5]
f = Floor.open(dwg, work, floor_id=floor_id, standard=std)
ov = Overlay.open(f, "proposal")              # never the deliverable before approval
res = place_device(f, ov, "R07", "socket_twin_usb", "pair:bed_head", dry_run=True)
# circuit_run(f, ov, [handles...], "C1", start="<board handle or x,y>", ring=True, dry_run=True)
# ov.remove([handles...]); ov.save()
print(json.dumps(res, indent=1, default=str))
PY
```

`dry_run=True` only proposes. Set it to `False` to write the proposal overlay, then render it
with `fl render --overlay proposal`.
