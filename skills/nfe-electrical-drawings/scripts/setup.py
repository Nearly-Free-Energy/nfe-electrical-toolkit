"""Install or update the NFE electrical toolkit on this computer, at the release this skill pins.

    python3 -I setup.py           (macOS, Linux)    clone or update the toolkit, build its venv,
    py -3 -I setup.py             (Windows)         run the synthetic tests
    ... setup.py --check                            report what is installed and change nothing

The code comes from GitHub (Nearly-Free-Energy/nfe-electrical-toolkit); this script holds none of it.
Overrides, normally unset: NFE_TOOLKIT_REF (release tag), NFE_TOOLKIT_REPO (git URL),
NFE_TOOLKIT_HOME (install folder), NFE_WORKDIR (work folder), NFE_PYTHON (Python to build the venv).
Exit codes: 0 ready; 1 failed; 2 a prerequisite is missing (git, Python 3.11+, LibreDWG);
3 (--check only) the toolkit needs installing or updating.
Standard library only, and runs on Python 3.8+ (macOS's own python3 is 3.9), so it can find a
newer Python for the venv. Tested on macOS; not yet run on Windows.
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
from pathlib import Path

PINNED_REF = "v0.1.0"  # the toolkit release this skill was written and tested against
GITHUB = "https://github.com/Nearly-Free-Energy/nfe-electrical-toolkit.git"
WINDOWS = os.name == "nt"
MACOS = sys.platform == "darwin"
REF = os.environ.get("NFE_TOOLKIT_REF") or PINNED_REF
REPO = os.environ.get("NFE_TOOLKIT_REPO") or GITHUB
DEST = Path(os.environ.get("NFE_TOOLKIT_HOME") or Path.home() / "nfe-electrical-toolkit").expanduser()
WORK = Path(os.environ.get("NFE_WORKDIR") or Path.home() / "nfe-electrical-work").expanduser()
VENV_PY = DEST / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")
LIBREDWG_WIN_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Programs" / "LibreDWG"
CHECK_311 = "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"

HINTS = {
    "git": ("xcode-select --install   (or: brew install git)" if MACOS else
            "install Git for Windows from https://git-scm.com/download/win" if WINDOWS else
            "install git with your distribution's package manager"),
    "python": ("brew install python@3.13   (or the installer from https://www.python.org/downloads/)" if MACOS else
               "install Python 3.13 from https://www.python.org/downloads/windows/ (it includes the py launcher)"
               if WINDOWS else "install python3.11 or newer with your distribution's package manager"),
    "libredwg": ("brew install libredwg" if MACOS else
                 "unzip libredwg-0.14-win64.zip from https://github.com/LibreDWG/libredwg/releases/tag/0.14 into "
                 f"{LIBREDWG_WIN_DIR} (see references/windows.md)" if WINDOWS else
                 "install your distribution's libredwg package, or build it from https://www.gnu.org/software/libredwg/"),
}


def out(tag: str, text: str) -> None:
    print(f"{tag:<6} {text}", flush=True)


def die(text: str, code: int = 1) -> None:
    out("[FAILED]", text)
    print("RESULT: failed", flush=True)
    sys.exit(code)


def on_path(name: str) -> str | None:
    """`name` in an absolute PATH directory. Like the toolkit, never the current folder, which on
    Windows would otherwise be searched first."""
    names = [name] if not WINDOWS or name.lower().endswith(".exe") else [name + ".exe", name]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        d = d.strip().strip('"')
        if d and Path(d).is_absolute():
            for n in names:
                cand = Path(d) / n
                if cand.is_file() and os.access(cand, os.X_OK):
                    return str(cand)
    return None


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", **kw)


def git(*args: str) -> subprocess.CompletedProcess:
    return run([GIT, "-C", str(DEST), *args])


# ---------------------------------------------------------------- prerequisites
def py_ok(cmd: list) -> bool:
    try:
        return run(cmd + ["-I", "-c", CHECK_311], timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def find_python() -> list | None:
    """A Python 3.11+ to build the venv with, as a command list (the py launcher needs an argument)."""
    cands = [[os.environ["NFE_PYTHON"]]] if os.environ.get("NFE_PYTHON") else []
    if WINDOWS:
        py = on_path("py")
        if py:
            listing = run([py, "-0p"]).stdout  # " -V:3.13 *   C:\...\python.exe", or " -3.10-64 ..." (older launchers)
            vers = sorted({tuple(map(int, m)) for m in re.findall(r"-(?:V:)?(\d+)\.(\d+)", listing)}, reverse=True)
            cands += [[py, f"-{a}.{b}"] for a, b in vers] + [[py, "-3"]]
        cands += [[p] for p in (on_path("python"), on_path("python3")) if p]
    else:
        names = ["python3.14", "python3.13", "python3.12", "python3.11", "python3"]
        cands += [[p] for p in map(on_path, names) if p]
        cands += [[f"{d}/{n}"] for d in ("/opt/homebrew/bin", "/usr/local/bin") for n in names[:-1]
                  if os.access(f"{d}/{n}", os.X_OK)]
    if sys.version_info >= (3, 11):
        cands.append([sys.executable])
    return next((c for c in cands if py_ok(c)), None)


def find_dwg2dxf() -> tuple[str | None, str]:
    """dwg2dxf, searched as the toolkit's convert.find_tool does. Returns (path, problem)."""
    exe = "dwg2dxf.exe" if WINDOWS else "dwg2dxf"
    env = os.environ.get("NFE_DWG2DXF", "").strip().strip('"')
    if env:  # when set, the toolkit uses it or stops: no fallback
        for cand in ((env, env + ".exe") if WINDOWS else (env,)):
            if Path(cand).is_file():
                return cand, ""
        return None, f"NFE_DWG2DXF is set to {env!r}, which is not a file: point it at {exe} or unset it"
    found = on_path(exe)
    if found:
        return found, ""
    for d in [LIBREDWG_WIN_DIR] if WINDOWS else [Path(p) for p in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")]:
        if (d / exe).is_file():
            return str(d / exe), ""
    return None, HINTS["libredwg"]


def work_in_git() -> str | None:
    """The git repository the work folder would sit in, unless the repository ignores it."""
    d = WORK
    while not d.is_dir() and d != d.parent:
        d = d.parent
    top = run([GIT, "-C", str(d), "rev-parse", "--show-toplevel"])
    if top.returncode != 0:
        return None
    ignored = run([GIT, "-C", top.stdout.strip(), "check-ignore", "-q", str(WORK)])
    return None if ignored.returncode == 0 else top.stdout.strip()


# ---------------------------------------------------------------- the checkout
def repo_key(url: str) -> str:
    """https and ssh spellings of the same GitHub repository compare equal."""
    u = url.strip().rstrip("/")
    u = u[:-4] if u.endswith(".git") else u
    for prefix in ("git@github.com:", "ssh://git@github.com/", "https://github.com/"):
        if u.startswith(prefix):
            u = "github.com/" + u[len(prefix):]
    return u.lower()


def resolve_ref() -> str | None:
    for spec in (f"refs/tags/{REF}^{{commit}}", f"refs/remotes/origin/{REF}^{{commit}}", f"{REF}^{{commit}}"):
        r = git("rev-parse", "-q", "--verify", spec)
        if r.returncode == 0:
            return r.stdout.strip()
    return None


def head() -> str:
    return git("rev-parse", "HEAD").stdout.strip()


def describe_head() -> str:
    r = git("describe", "--tags", "--exact-match", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else git("rev-parse", "--short", "HEAD").stdout.strip()


def checkout_problem() -> str:
    """Why an existing folder can't be updated (empty when it can): never overwrite someone's work."""
    if not (DEST / ".git").exists():
        return f"{DEST} exists but is not a git clone of the toolkit. Move it aside, or set NFE_TOOLKIT_HOME to another folder."
    origin = git("remote", "get-url", "origin").stdout.strip()
    if repo_key(origin) != repo_key(REPO):
        return f"{DEST} is a clone of {origin or 'an unknown repository'}, not {REPO}. Set NFE_TOOLKIT_HOME to another folder."
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        return (f'{DEST} has local changes to the toolkit\'s files (git -C "{DEST}" status). The setup won\'t '
                "overwrite them: commit them to a branch or discard them first.")
    branch = git("symbolic-ref", "-q", "--short", "HEAD")
    if branch.returncode == 0:
        return (f"{DEST} has branch '{branch.stdout.strip()}' checked out (a development clone?). The setup only "
                f'manages detached release checkouts: run git -C "{DEST}" checkout --detach {REF} yourself, '
                "or set NFE_TOOLKIT_HOME to another folder.")
    return ""


def venv_ok(import_toolkit: bool = True) -> bool:
    code = CHECK_311 if not import_toolkit else "import nfe_electrical; " + CHECK_311
    return VENV_PY.is_file() and run([str(VENV_PY), "-I", "-c", code]).returncode == 0


def tail(text: str, n: int) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


# ---------------------------------------------------------------- main
def main() -> None:
    global GIT
    check = sys.argv[1:] == ["--check"]
    if sys.argv[1:] and not check:
        print(__doc__ if sys.argv[1] in ("-h", "--help") else f"unknown option: {sys.argv[1]} (use --check or --help)")
        sys.exit(0 if sys.argv[1] in ("-h", "--help") else 1)
    print(f"NFE electrical toolkit: pinned release {REF} ({'check' if check else 'install'})", flush=True)
    out("", f"toolkit folder: {DEST}")
    out("", f"work folder:    {WORK}")

    missing = []
    GIT = on_path("git")
    version = run([GIT, "--version"]) if GIT else None
    if version is not None and version.returncode == 0:
        out("[ok]", version.stdout.strip())
    else:
        out("[MISSING]", f"git: {HINTS['git']}")
        missing.append("git")
    py = find_python()
    if py:
        out("[ok]", f"Python {run(py + ['-I', '-c', 'import platform; print(platform.python_version())']).stdout.strip()} ({' '.join(py)})")
    else:
        out("[MISSING]", f"Python 3.11 or newer: {HINTS['python']}")
        missing.append("python")
    dwg2dxf, dwg_problem = find_dwg2dxf()
    if dwg2dxf:
        v = run([dwg2dxf, "--version"]).stdout.strip().splitlines()
        out("[ok]", f"LibreDWG: {v[0] if v else 'dwg2dxf'} ({dwg2dxf})")
    else:
        out("[MISSING]", f"LibreDWG dwg2dxf, needed to open .dwg files: {dwg_problem}")
    # Client drawings and outputs live in the work folder, so no git repository may track it.
    work_repo = work_in_git() if GIT else None
    if work_repo:
        out("[MISSING]", f"the work folder is inside the git repository {work_repo}, and client files must never go "
                         "into git: set NFE_WORKDIR to a folder outside any repository")

    if check:
        target = None
        if not DEST.exists():
            status, code = "not installed", 3
        elif not GIT:
            status, code = "can't check the install without git", 2
        elif checkout_problem():
            status, code = f"blocked: {checkout_problem()}", 1
        elif not venv_ok():
            status, code = f"incomplete (no working virtual environment in {DEST / '.venv'})", 3
        elif (target := resolve_ref()) and head() == target:
            status, code = f"installed {describe_head()}, matches the pinned release {REF}", 0
        else:
            status, code = f"installed {describe_head()}, but this skill pins {REF}: run setup.py to update", 3
        print(f"STATUS: {status}")
        if code == 3 and missing:
            code = 2
        if code == 0 and not dwg2dxf:
            print("STATUS: LibreDWG missing, so .dwg files can't be opened")
            code = 2
        if work_repo:
            print(f"STATUS: blocked: move the work folder out of {work_repo}")
            code = 1
        sys.exit(code)

    if missing:
        die("install the missing prerequisites above, then run this again.", 2)
    if work_repo:
        die(f"choose a work folder outside {work_repo} (NFE_WORKDIR).")

    cloned = False
    if not DEST.exists():
        print(f"Cloning {REPO} ...", flush=True)
        r = run([GIT, "clone", "--quiet", REPO, str(DEST)])
        if r.returncode != 0:
            print(tail(r.stderr, 5))
            die(f"git clone failed (network, or the URL {REPO}).")
        cloned = True
    elif checkout_problem():
        die(checkout_problem())
    print("Fetching releases ...", flush=True)
    r = git("fetch", "--tags", "origin")
    if r.returncode != 0:
        print(tail(r.stdout + r.stderr, 5))
        if "would clobber existing tag" in r.stdout + r.stderr:
            die(f"a release tag on {REPO} was moved after it was installed here (see above), so the setup stops "
                "rather than trust it. Ask the toolkit's maintainer before going on.")
        die(f"git fetch from {REPO} failed (network?).")
    target = resolve_ref()
    if not target:
        die(f"release {REF} not found in {REPO}.")
    before = head()
    if before != target or cloned:
        r = git("-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", target)
        if r.returncode != 0:
            print(tail(r.stderr, 5))
            die(f"could not check out {REF}.")
    out("[ok]", f"cloned {describe_head()}" if cloned else
        f"already at {describe_head()}" if before == target else f"updated to {describe_head()}")

    # ------------------------------------------------------------ venv and package
    if venv_ok(import_toolkit=False):
        out("[ok]", f"virtual environment: reusing {DEST / '.venv'}")
    else:
        print("Creating the virtual environment ...", flush=True)
        r = run(py + ["-I", "-m", "venv", "--clear", str(DEST / ".venv")])
        if r.returncode != 0:
            print(tail(r.stderr, 5))
            die(f"could not create {DEST / '.venv'}.")
        out("[ok]", f"virtual environment: created with {' '.join(py)}")
    print("Installing the toolkit and its dependencies (pip install -e; a few minutes the first time) ...", flush=True)
    r = run([str(VENV_PY), "-I", "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
             "--editable", f"{DEST}[test]"])
    if r.returncode != 0:
        print(tail(r.stdout + r.stderr, 15))
        die(f"pip install failed (network?). The checkout is at {REF} already: run this script again to finish.")
    r = run([str(VENV_PY), "-I", "-c",
             "import importlib.metadata as md, nfe_electrical\n"
             "print('[ok]   nfe_electrical %s from %s' % (nfe_electrical.__version__, nfe_electrical.__file__))\n"
             "print('       ' + ', '.join('%s %s' % (p, md.version(p)) for p in "
             "('ezdxf', 'shapely', 'mcp', 'numpy', 'scipy', 'matplotlib', 'PyYAML', 'pytest')))"])
    if r.returncode != 0:
        print(tail(r.stderr, 10))
        die("the toolkit does not import after installing.")
    print(r.stdout.rstrip(), flush=True)
    WORK.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ synthetic tests
    print("Running the synthetic test suite ...", flush=True)
    r = run([str(VENV_PY), "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_synthetic.py"],
            cwd=str(DEST))
    if r.returncode != 0:
        print(tail(r.stdout + r.stderr, 40))
        die("the synthetic tests failed: don't use this install for project work.")
    out("[ok]", f"synthetic tests: {tail(r.stdout, 1)}")

    print()
    print(f'CLI:  "{VENV_PY}" -I -m nfe_electrical.cli --help')
    print(f'MCP (optional, Claude Code): claude mcp add nfe-electrical --scope user -- "{VENV_PY}" -I -m '
          f'nfe_electrical.mcp_server --workdir "{WORK}"')
    if not dwg2dxf:
        print(f"RESULT: toolkit {REF} installed and tested, but LibreDWG is missing, so .dwg files can't be opened yet: "
              f"{dwg_problem}")
        sys.exit(2)
    print(f"RESULT: ready. Toolkit {REF} in {DEST}, work folder {WORK}")


GIT: str | None = None

if __name__ == "__main__":
    main()
