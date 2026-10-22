"""Package a skill from this folder as the zip a Claude Team owner uploads.

    python3 -I skills/build_zip.py [skills/nfe-electrical-drawings] [--out dist]

The zip holds the skill folder at its root (nfe-electrical-drawings/SKILL.md, scripts/,
references/). It leaves out the test cases in evals/ and caches, and uses fixed timestamps,
so the same commit always gives the same zip. SKILL.md's frontmatter is checked against the
upload rules first. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKIP_DIRS = {"evals", "__pycache__"}
SKIP_FILES = {".DS_Store"}
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def frontmatter(skill_md: Path) -> dict[str, str]:
    """Top-level keys of SKILL.md's frontmatter; values must be on one line (plain or double-quoted)."""
    m = re.match(r"^---\n(.*?)\n---\n", skill_md.read_text(encoding="utf-8"), re.S)
    if not m:
        raise SystemExit(f"{skill_md}: no YAML frontmatter")
    out = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if not sep or line[:1].isspace():
            continue
        value = value.strip()
        if value in (">", "|", ">-", "|-"):
            raise SystemExit(f"{skill_md}: write {key.strip()} on one line")
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"')
        out[key.strip()] = value
    return out


def check(skill: Path) -> list[str]:
    fm = frontmatter(skill / "SKILL.md")
    name, desc = fm.get("name", ""), fm.get("description", "")
    problems = []
    if name != skill.name:
        problems.append(f"name {name!r} differs from the folder name {skill.name!r}")
    if not NAME.match(name) or len(name) > 64 or "anthropic" in name or "claude" in name:
        problems.append(f"name {name!r}: up to 64 lowercase letters, digits and single hyphens, without 'anthropic' or 'claude'")
    if not desc or len(desc) > 1024 or "<" in desc or ">" in desc:
        problems.append(f"description: 1-1024 characters without angle brackets (it has {len(desc)})")
    extra = [p for p in skill.rglob("SKILL.md") if p != skill / "SKILL.md" and not skipped(p.relative_to(skill))]
    if extra:
        problems.append(f"only one SKILL.md is allowed: {', '.join(map(str, extra))}")
    return problems


def skipped(rel: Path) -> bool:
    return bool(SKIP_DIRS & set(rel.parts[:-1])) or rel.name in SKIP_FILES or rel.suffix == ".pyc"


def build(skill: Path, out_dir: Path) -> Path:
    files = sorted(p for p in skill.rglob("*") if p.is_file() and not skipped(p.relative_to(skill)))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{skill.name}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            info = zipfile.ZipInfo(f"{skill.name}/{p.relative_to(skill).as_posix()}", date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = (0o100755 if p.suffix == ".sh" else 0o100644) << 16  # regular file + mode
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, p.read_bytes())
            print(f"  {info.filename}")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="Package a skill folder as an uploadable zip")
    ap.add_argument("skill", nargs="?", default=str(HERE / "nfe-electrical-drawings"))
    ap.add_argument("--out", default=str(HERE.parent / "dist"), help="output folder (default: dist/, git-ignored)")
    args = ap.parse_args()
    skill = Path(args.skill).resolve()
    problems = check(skill)
    if problems:
        raise SystemExit("not packaged:\n  " + "\n  ".join(problems))
    setup = skill / "scripts" / "setup.py"
    pinned = re.search(r'^PINNED_REF = "([^"]+)"', setup.read_text(), re.M) if setup.is_file() else None
    path = build(skill, Path(args.out).resolve())
    print(f"{path}  ({path.stat().st_size} bytes, sha256 {hashlib.sha256(path.read_bytes()).hexdigest()[:16]})")
    if pinned:
        print(f"installs toolkit release {pinned.group(1)}")


if __name__ == "__main__":
    main()
