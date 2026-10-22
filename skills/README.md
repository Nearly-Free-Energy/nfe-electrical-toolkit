# Claude skill: nfe-electrical-drawings

`nfe-electrical-drawings/` is an organisation skill for NFE's Claude Team plan. It lets any NFE
engineer use this toolkit from the Claude desktop app's Code tab or from Claude Code. The code
stays in this repository. The skill only installs it, at a pinned release, and teaches Claude
the workflow and the ground rules.

| File (in `skills/`) | Purpose |
|---|---|
| `nfe-electrical-drawings/SKILL.md` | The workflow and ground rules Claude follows: open a floor with its governing standard, find rooms and run checks, propose with dry runs and renders, write after approval, validate, plot sheets, deliver DXF + PDF |
| `nfe-electrical-drawings/scripts/setup.py` | Installs or updates the toolkit at `PINNED_REF` into `~/nfe-electrical-toolkit`, builds its venv (`pip install -e ".[test]"`), checks LibreDWG and runs the synthetic tests. `--check` only reports. Standard-library Python, run with `python3` on macOS or `py -3` on Windows (untested there) |
| `nfe-electrical-drawings/references/mcp.md` | The optional MCP server, its configuration, and the Python API for what the CLI lacks |
| `nfe-electrical-drawings/references/windows.md` | Windows steps. The toolkit is CI-tested on Windows; `setup.py` hasn't been run there yet |
| `nfe-electrical-drawings/evals/` | Test cases for the skill and the synthetic floor they use. Not packaged. |
| `build_zip.py` | Builds the zip a Team owner uploads |
| `test_setup.sh` | Clean-install test of `setup.py` on macOS or Linux: an empty HOME, no pip cache, then the update and refusal paths, all written to a log |

## Build the zip

```bash
python3 -I skills/build_zip.py
```

This writes `dist/nfe-electrical-drawings.zip`, with the skill folder at the zip's root and
`evals/` left out. It checks the frontmatter first, and prints the toolkit release the zip pins.
The same commit always gives the same bytes.

## Provision it for the organisation (Team owner)

Only an Owner (Primary Owner or Owner) can add or remove organisation-wide skills.

1. **Check the policy once.** In claude.ai, go to **Organization settings > Plugins & skills**,
   open the **Policy** tab, and make sure **Cloud code execution and file creation** and
   **Skills** are on. Skills need both.
2. **Get the zip.** Download `nfe-electrical-drawings.zip` from the GitHub release for the
   pinned tag (v0.1.0), or build it from that tag with `python3 -I skills/build_zip.py`.
   Its root must be the `nfe-electrical-drawings/` folder, with `SKILL.md` inside.
3. **Upload it.** On **Organization settings > Plugins & skills**, click **Add** (top right),
   choose **Upload a skill**, and select the zip. It is provisioned to every member at once and
   is on by default. Members can switch it off for themselves, but they can't delete it.
4. **Check it reaches the engineers.**
   - In the Claude desktop app, open a Code-tab session and ask: *"Set up the NFE electrical
     toolkit."* Claude should load `nfe-electrical-drawings` and run `setup.py --check`.
   - Claude Code in a terminal gets the skill automatically (v2.1.273 or later, signed in with
     `/login` to the claude.ai account). It is synced into `~/.claude/skills/synced/`.
   - Where syncing is off (an API-key login, or `syncClaudeAiSkills: false`), copy the
     `nfe-electrical-drawings/` folder to `~/.claude/skills/` instead.
   - If the skill appears in chat but not in local Code-tab sessions, check whether the
     organisation's managed settings set `disableSideloadFlags`. Anthropic's desktop docs say
     that setting stops local sessions loading account skills.
5. **Update it** when a new release is pinned. There is no in-place replace for an
   Owner-uploaded skill, so remove the old one first: on the **Inventory** tab, choose
   **Remove from library** from its row's menu. Then upload the new zip. Removing the skill
   doesn't touch anyone's installed toolkit or work folder. Each engineer's next session finds
   the new pin and updates the toolkit.

The skill has to work on the engineer's own computer. In a claude.ai chat, which runs in
Anthropic's sandbox, it only explains that it needs the desktop app's Code tab or Claude Code.

## Release a new toolkit version

1. On `main`, set the new version in `pyproject.toml`, `src/nfe_electrical/__init__.py` and
   `PINNED_REF` in `skills/nfe-electrical-drawings/scripts/setup.py`, and commit. The tagged
   commit then pins itself.
2. Tag the commit and push the tag: `git tag -a v0.2.0 -m "..."`, then
   `git push origin v0.2.0`. Don't move or delete a release tag once engineers have installed
   it. `setup.py` refuses a tag that changed upstream ("would clobber existing tag"). A GitHub
   tag ruleset for `v*` that blocks updates and deletions makes that hard to do by accident.
3. Run the clean-install test, `bash skills/test_setup.sh`, which clones from GitHub at the
   new pin. It must end `ALL PASSED`. To test before pushing the tag, run it against a local
   bare clone that carries the tag: `bash skills/test_setup.sh --repo file:///path/to/bare.git`.
4. Build the zip, attach it to a GitHub release for the tag, and have a Team owner upload it
   in place of the previous version.
5. Each engineer's next session finds the new pin (`setup.py --check` exits 3, "needs
   updating"), and Claude runs the update: fetch, check out the new tag, reinstall, re-test.

## Why a clone and a venv rather than `uvx`

The simpler route would be
`uvx --from git+https://github.com/Nearly-Free-Energy/nfe-electrical-toolkit nfe-elec-mcp` as
the MCP server command. It was evaluated and not adopted, for these reasons:

- **The rule sets would be missing.** uvx installs a wheel built from the repository, and
  `rules/` is not package data. A wheel built from this repository holds `nfe_electrical/`
  and its `data/*.yaml` but no `rules/` files (checked with `pip wheel --no-deps .`), so the
  server's `apply_rules` would have no rule sets to point at.
- **Nothing pins what engineers run.** Without `@<tag>`, uvx runs whatever is on `main`,
  unreviewed. With `...@v0.1.0`, uv looks the tag up on GitHub at every start, so an offline
  start is unreliable.
- **Dependencies drift.** `uvx --from git+...` ignores lock files and resolves dependencies on
  each machine at its first run, so two engineers can run different ezdxf, shapely or mcp
  versions. The toolkit has already been caught out once by mcp 2.x renaming FastMCP.
- **The first start times out.** A cold start downloads about 65 MB (numpy, scipy, matplotlib,
  shapely, ezdxf, mcp) and builds the environment before the server answers. On this Mac's
  connection that took 6–7 minutes. Claude Code's MCP startup limit is 30 s (`MCP_TIMEOUT`),
  and users report the desktop app giving up at 60 s. The server in the venv starts in about
  2 s.
- **It adds a tool and a second install.** Each machine would need uv
  (`brew install uv`, or `winget install --id=astral-sh.uv -e`). The skill's Bash CLI still
  needs the clone and venv, and the two installs could run different versions.

It becomes reasonable once the rule sets ship inside the package, releases are tagged, and an
engineer uses only the MCP server. Then pin a tag and warm the cache first
(`uv tool install git+https://github.com/Nearly-Free-Energy/nfe-electrical-toolkit@vX.Y.Z`,
then point the MCP config at the installed `nfe-elec-mcp`). A one-click desktop extension that
bundles everything is the other way to get there.

## Test the skill

`evals/` holds three test cases: a clean setup, a propose-before-writing session, and a drawing
that contains instructions plus a request to commit client files to git. Each case has
`prompt.md`, `case.yaml` and `graders/`. They need the synthetic floor from
`evals/make_test_floor.py`, which you run with the toolkit's venv Python. With Claude Code
v2.1.269 or later you can run them from the skill folder with
`claude plugin eval . --allow-tools Write,Edit,Bash`. They are real, billed model runs.
