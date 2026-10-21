"""Workspace layout and path safety.

    <workdir>/input/<floor_id>/<drawing>.dwg   read-only copies of received drawings (untrusted data)
    <workdir>/cache/                           dwg2dxf output, keyed by file hash
    <workdir>/<floor_id>/                      everything the toolkit writes for that floor

The toolkit only ever writes inside <workdir>. Received drawings are copied into a
new, empty input directory and made read-only; the originals are never opened for writing.
"""
from __future__ import annotations

import re
import shutil
import stat
from pathlib import Path

from .convert import sha256_file

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


def check_id(value: str, what: str = "id") -> str:
    if not _ID.match(value or ""):
        raise ValueError(f"invalid {what} {value!r}: use 1-40 letters, digits, '-' or '_'")
    return value


def slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return (s or "floor")[:40]


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "input").mkdir(exist_ok=True)
        (self.root / "cache").mkdir(exist_ok=True)

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    def floor_dir(self, floor_id: str, *parts: str) -> Path:
        p = self.inside(self.root / check_id(floor_id, "floor id"), *parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def inside(self, base: Path, *parts: str) -> Path:
        p = Path(base, *parts).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"path {p} is outside the workspace {self.root}")
        return p

    def import_drawing(self, src: str | Path, floor_id: str) -> Path:
        """Copy a received drawing into input/<floor_id>/ (new and empty unless it already holds this file)."""
        src = Path(src).expanduser().resolve()
        if not src.is_file():
            raise FileNotFoundError(src)
        if src.suffix.lower() not in (".dwg", ".dxf"):
            raise ValueError(f"{src.name}: only .dwg or .dxf drawings are accepted")
        dest_dir = self.root / "input" / check_id(floor_id, "floor id")
        if src.parent == dest_dir:
            return src
        dest = dest_dir / src.name
        if dest_dir.exists():
            existing = [p for p in dest_dir.iterdir()]
            if existing and not (len(existing) == 1 and existing[0] == dest and sha256_file(dest) == sha256_file(src)):
                raise FileExistsError(f"input directory {dest_dir} already holds other files; use another floor id")
            if dest.exists():
                return dest
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        dest.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        return dest
