"""DWG -> DXF conversion with LibreDWG's `dwg2dxf`.

LibreDWG is GPL-3.0; the toolkit only runs it as a separate program, it does not
link to it. Drawings are untrusted input, so the converter runs with a timeout,
from a neutral working directory, and never writes next to the source file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ACAD_VERSIONS = {
    "AC1015": "AutoCAD 2000", "AC1018": "AutoCAD 2004", "AC1021": "AutoCAD 2007",
    "AC1024": "AutoCAD 2010", "AC1027": "AutoCAD 2013", "AC1032": "AutoCAD 2018",
}
_MAGIC = re.compile(rb"^AC\d{4}")
_COMMON_PATHS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")


class ConversionError(RuntimeError):
    pass


@dataclass
class Conversion:
    source: str
    dxf: str
    sha256: str
    acad_version: str
    seconds: float
    cached: bool
    exit_code: int
    warnings: int
    errors: int
    log: str

    def as_dict(self) -> dict:
        return asdict(self)


def find_tool(name: str) -> str:
    """Locate a LibreDWG program: $NFE_<NAME>, then PATH, then the usual Homebrew/Linux places."""
    env = os.environ.get("NFE_" + name.upper().replace("-", "_"))
    if env and Path(env).is_file():
        return env
    found = shutil.which(name)
    if found:
        return found
    for d in _COMMON_PATHS:
        cand = Path(d) / name
        if cand.is_file():
            return str(cand)
    raise ConversionError(f"{name} not found; install LibreDWG (e.g. `brew install libredwg`) or set NFE_{name.upper()}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dwg_version(path: Path) -> str:
    with open(path, "rb") as fh:
        head = fh.read(6)
    if not _MAGIC.match(head):
        raise ConversionError(f"{path.name} does not look like a DWG file (header {head!r})")
    return head.decode("ascii")


def convert_dwg(dwg: Path, cache_dir: Path, timeout_s: int = 600, force: bool = False) -> Conversion:
    """Convert `dwg` to DXF inside `cache_dir`, reusing an earlier result for the same file hash."""
    dwg = Path(dwg).resolve()
    cache_dir = Path(cache_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    if cache_dir == dwg.parent:
        raise ConversionError("cache directory must not be the drawing's own directory")
    version = dwg_version(dwg)
    digest = sha256_file(dwg)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", dwg.stem)[:60]
    out = cache_dir / f"{stem}-{digest[:12]}.dxf"
    meta_path = out.with_suffix(".json")
    log_path = out.with_suffix(".dwg2dxf.log")
    if out.is_file() and meta_path.is_file() and not force:
        meta = json.loads(meta_path.read_text())
        meta["cached"] = True
        return Conversion(**meta)

    exe = find_tool("dwg2dxf")
    tmp = out.with_suffix(".partial.dxf")
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            [exe, "-y", "-o", str(tmp), str(dwg)], cwd=str(cache_dir), stdin=subprocess.DEVNULL,
            capture_output=True, timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        tmp.unlink(missing_ok=True)
        raise ConversionError(f"dwg2dxf timed out after {timeout_s} s on {dwg.name}") from exc
    seconds = time.perf_counter() - t0
    text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    log_path.write_text(text, encoding="utf-8")
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise ConversionError(f"dwg2dxf failed on {dwg.name} (exit {proc.returncode}); see {log_path}")
    tmp.replace(out)
    conv = Conversion(
        source=str(dwg), dxf=str(out), sha256=digest, acad_version=f"{version} ({ACAD_VERSIONS.get(version, 'unknown')})",
        seconds=round(seconds, 2), cached=False, exit_code=proc.returncode,
        warnings=sum(1 for ln in text.splitlines() if ln.startswith("Warning")),
        errors=sum(1 for ln in text.splitlines() if ln.startswith("ERROR")), log=str(log_path),
    )
    meta_path.write_text(json.dumps(conv.as_dict(), indent=1))
    return conv
