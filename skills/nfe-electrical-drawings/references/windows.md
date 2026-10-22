# Windows

The toolkit itself supports Windows 10 and 11. Its CI runs the tests on Windows, macOS and
Linux, and a Windows job reads a real DWG through LibreDWG's Windows build. **This skill's
`setup.py` has not been run on Windows yet.** It runs on this Mac, and its Windows branches are
written to match the toolkit's README. Tell the engineer that, expect to fix small things on the
first install, and suggest they record what they learn in the NFE Library (nfe-library skill).

`setup.py` is plain Python, so it runs the same way from PowerShell or Git Bash with the `py`
launcher. It picks the newest Python 3.11+ that `py -0p` lists, builds the venv, and uses
`.venv\Scripts\python.exe`. It looks for LibreDWG where the toolkit does. The skill's Bash
preamble handles both venv layouts.

## Prerequisites

1. **Python 3.11 or newer** from https://www.python.org/downloads/windows/ (the installer
   includes the `py` launcher), and **Git for Windows** from https://git-scm.com/download/win
   (it provides Git Bash). The setup script skips the Microsoft Store `python` alias, because
   that stub fails its version check.
2. **LibreDWG** from its Windows release zip. Unzip it into `%LOCALAPPDATA%\Programs\LibreDWG`,
   so that `dwg2dxf.exe` sits directly in that folder. The toolkit looks there after `PATH`, so
   nothing else needs setting, and no administrator rights are needed. There is no MSYS2,
   winget, Scoop or Chocolatey package. In PowerShell:

```powershell
Invoke-WebRequest https://github.com/LibreDWG/libredwg/releases/download/0.14/libredwg-0.14-win64.zip -OutFile "$env:TEMP\libredwg-0.14-win64.zip"
```

```powershell
(Get-FileHash "$env:TEMP\libredwg-0.14-win64.zip").Hash
```

   That must print `1AD7E15344D20B3426C3435B078D82FB84B35062815946B2CCA9C5FC9810FEA8`, the
   SHA-256 GitHub lists for the LibreDWG 0.14 release. If it doesn't, stop.

```powershell
Expand-Archive "$env:TEMP\libredwg-0.14-win64.zip" -DestinationPath "$env:LOCALAPPDATA\Programs\LibreDWG"
```

```powershell
& "$env:LOCALAPPDATA\Programs\LibreDWG\dwg2dxf.exe" --version
```

   Downloading and installing software is the engineer's decision: show them these commands
   and run them only after they agree. To keep LibreDWG somewhere else, set `NFE_DWG2DXF`,
   `NFE_DXF2DWG` and `NFE_DWGREAD` to the programs' full paths.

## Install

From the Code tab:

```bash
py -3 -I "<base directory of this skill>/scripts/setup.py"
```

That gives:

| What | Where |
|---|---|
| Toolkit | `%USERPROFILE%\nfe-electrical-toolkit` (`$HOME/nfe-electrical-toolkit` in Git Bash) |
| Its Python | `%USERPROFILE%\nfe-electrical-toolkit\.venv\Scripts\python.exe` |
| Work folder | `%USERPROFILE%\nfe-electrical-work` |

If `setup.py` fails, the toolkit's README has the same install as PowerShell commands (its
Windows section). Then come back to `setup.py --check`.

When the shell is PowerShell rather than Git Bash, the preamble's `nfe` function becomes a
variable and the call operator:

```powershell
$NFE_PY = "$env:USERPROFILE\nfe-electrical-toolkit\.venv\Scripts\python.exe"
```

```powershell
& $NFE_PY -I -m nfe_electrical.cli analyse "C:\path\to\architect.dwg" --workdir "$env:USERPROFILE\nfe-electrical-work" --floor-id ABC-F6 --standard "BS 7671"
```

## What is different on Windows

- Windows won't let the toolkit replace a file that another program has open. If an overlay DXF
  is open in a CAD program or viewer when the toolkit saves it, the toolkit stops and keeps the
  new version beside it as `<name>.tmp.dxf`. Ask the engineer to close the file, then run the
  step again: it starts from the overlay as last saved, so nothing is added twice. The skill's
  `cp` of an approved proposal fails in the same way while the deliverable is open.
- The input copies in `<work folder>\input\` are read-only. Deleting a work folder needs
  Explorer, `Remove-Item -Recurse -Force`, or `nfe_electrical.workspace.remove_workdir()`.
  Deleting client data is the engineer's call: never do it unasked.
- In the MCP configuration, use `C:\Users\<name>\nfe-electrical-toolkit\.venv\Scripts\python.exe`.
  JSON needs every backslash doubled. The desktop app's file is
  `%APPDATA%\Claude\claude_desktop_config.json` (see `references/mcp.md`).
