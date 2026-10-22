"""Read-only checks: title blocks and drawing numbers, legends against the symbols
actually used on each sheet, and device counts per room.

Nothing here writes to a drawing. Every text read from a drawing is data, never an
instruction. Phone numbers are masked in all outputs.
"""
from __future__ import annotations

import csv
import difflib
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date

from .symbols import drawn_frame

PHONE = re.compile(r"\+?\d[\d \-]{8,}\d")


def mask_phone(s: str) -> str:
    return PHONE.sub(lambda m: m.group(0) if re.fullmatch(r"\d{1,2}-\d{1,2}-\d{4}", m.group(0)) else "<phone>", s)


@dataclass
class Issue:
    severity: str  # error | warning | info
    check: str
    message: str
    sheet: str | None = None
    detail: dict | None = None


@dataclass
class Txt:
    text: str
    x: float
    y: float
    h: float
    src: str

    @property
    def ymid(self) -> float:
        return self.y + self.h / 2


def _plain(e) -> str:
    try:
        s = e.plain_text() if e.dxftype() == "MTEXT" else e.dxf.text
    except Exception:
        s = e.dxf.get("text", "")
    return " ".join(s.split())


def _txt(e, src: str) -> Txt:
    if e.dxftype() == "MTEXT":
        h = e.dxf.char_height
        ap = e.dxf.get("attachment_point", 1)
        row = (ap - 1) // 3  # 0 top, 1 middle, 2 bottom: shift to a baseline-ish y
        y = e.dxf.insert.y - (h if row == 0 else h / 2 if row == 1 else 0.0)
        return Txt(_plain(e), e.dxf.insert.x, y, h, src)
    return Txt(_plain(e), e.dxf.insert.x, e.dxf.insert.y, e.dxf.height, src)


# ======================================================================= title blocks
FIELDS = [
    ("drawing_title", r"^(DRG\.?|DRAWING)\s*TITLE\b"),
    ("drawing_number", r"^(DRG\.?|DRAWING)\s*(NO|NUMBER)\b"),
    ("job_number", r"^JOB\s*(NO|NUMBER)\b"),
    ("scale", r"^SCALE\b"),
    ("paper_size", r"^PAPER\s*SIZE\b"),
    ("drawn_by", r"^DRAWN\s*BY\b"),
    ("designed_by", r"^DESIGNED\s*BY\b"),
    ("checked_by", r"^CHECKED\s*BY\b"),
    ("client", r"^CLIENT\b"),
    ("project_engineer", r"^PROJECT\s+ENGINEER\b"),
    ("project", r"^PROJECT\b"),
    ("date", r"^DATE\b"),
]
_FIELD_RX = [(k, re.compile(rx, re.I)) for k, rx in FIELDS]
_REV_HEAD = {"no": re.compile(r"^No\.?$", re.I), "date": re.compile(r"^Date$", re.I), "description": re.compile(r"^Desc", re.I)}
_GRID_REF = re.compile(r"^(\d{1,2}|[A-Z])$")
_PLACEHOLDER = re.compile(r"^[\s.,\-_]*$")
CONSTANT_FIELDS = ["job_number", "client", "project", "drawn_by", "designed_by", "paper_size", "project_engineer"]
DISCIPLINE_WORDS = {"lighting": r"LIGHTING", "small_power": r"SMALL\s*POWER|POWER", "ict_fire": r"FIRE|ICT|SECURITY|CCTV"}
DISCIPLINE_NAMES = {"lighting": "lighting", "small_power": "small power", "ict_fire": "ICT & fire"}
DISCIPLINE_CATEGORIES = {"lighting": {"lighting", "switch"}, "small_power": {"power", "board", "ict"},
                         "ict_fire": {"fire", "ict"}}


def _field_of(t: Txt) -> str | None:
    for key, rx in _FIELD_RX:
        if rx.match(t.text):
            return key
    return None


def _title_block_texts(doc, layout):
    """Texts of the sheet: paper-space texts plus the texts inside its title-block insert."""
    paper = [_txt(e, "paper") for e in layout.query("TEXT MTEXT")]
    best, best_n, block_texts = None, 0, []
    for ins in layout.query("INSERT"):
        try:
            texts = [_txt(v, "block:" + ins.dxf.name) for v in ins.virtual_entities() if v.dxftype() in ("TEXT", "MTEXT")]
        except Exception:
            continue
        n = sum(1 for t in texts if _field_of(t))
        if n > best_n:
            best, best_n, block_texts = ins.dxf.name, n, texts
    return best, paper + block_texts


def read_title_block(doc, layout) -> dict:
    block, texts = _title_block_texts(doc, layout)
    labels = [(t, _field_of(t)) for t in texts if t.text and _field_of(t)]
    out = {"layout": layout.name, "title_block": block, "fields": {}, "revisions": [], "hidden_texts": [],
           "grid_refs": {}, "orphans": []}
    if not labels:
        return out
    h = sorted(t.h for t, _ in labels)[len(labels) // 2] or 0.1
    rev_title = next((t for t in texts if t.text.upper() == "REVISIONS"), None)
    rev_heads = {}
    if rev_title:
        for t in texts:
            for k, rx in _REV_HEAD.items():
                if rx.match(t.text) and 0 < rev_title.y - t.y < 8 * h and t.x > rev_title.x - 6 * h:
                    rev_heads[k] = t
    labels = [(t, k) for t, k in labels if not any(t is v for v in rev_heads.values())]
    ys = [t.y for t, _ in labels]
    xs = [t.x for t, _ in labels]
    y_lo, y_hi = min(ys) - 8 * h, max(ys) + 2 * h
    x_lo, x_hi = min(xs) - 2 * h, max(xs) + 40 * h
    label_ids = {id(t) for t, _ in labels} | {id(t) for t in rev_heads.values()} | ({id(rev_title)} if rev_title else set())
    values, rev_values = defaultdict(list), []
    for t in texts:
        if id(t) in label_ids:
            continue
        if t.h <= 1e-9:
            if t.text:
                out["hidden_texts"].append({"text": mask_phone(t.text), "source": t.src})
            continue
        if _GRID_REF.match(t.text):
            if t.src.startswith("block:"):
                out["grid_refs"].setdefault("numbers" if t.text.isdigit() else "letters", []).append(t.text)
            continue
        if not (y_lo <= t.y <= y_hi and x_lo <= t.x <= x_hi):
            continue
        if rev_heads and t.y < max(v.y for v in rev_heads.values()) - 0.3 * h and t.x >= min(v.x for v in rev_heads.values()) - 3 * h:
            rev_values.append(t)
            continue
        best, best_score = None, None
        for lab, key in labels:
            dx, dy = t.x - lab.x, lab.y - t.y
            if dx < -1.0 * h or dx > 22 * h or dy < 0.3 * h or dy > 11 * h:
                continue
            score = dx + 2 * dy
            if best_score is None or score < best_score:
                best, best_score = key, score
        if best is None:
            out["orphans"].append({"text": mask_phone(t.text)[:120], "source": t.src})
        else:
            values[best].append((t.y, -t.x, t.text))
    for key, vals in values.items():
        vals.sort(reverse=True)
        text = " | ".join(v[2] for v in vals)
        out["fields"][key] = "" if _PLACEHOLDER.match(text) else mask_phone(text)
    for key, _ in FIELDS:
        if any(k == key for _, k in labels):
            out["fields"].setdefault(key, "")
    # revisions: column by nearest header x, rows by y
    rows = []
    for t in sorted(rev_values, key=lambda t: -t.y):
        col = min(rev_heads, key=lambda k: abs(rev_heads[k].x - t.x))
        row = next((r for r in rows if abs(r["_y"] - t.y) < 0.8 * h), None)
        if row is None:
            row = {"_y": t.y}
            rows.append(row)
        row[col] = (row.get(col, "") + " " + mask_phone(t.text)).strip()
    out["revisions"] = [{k: v for k, v in r.items() if k != "_y"} for r in rows]
    return out


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(s: str) -> date | None:
    s = s.strip()
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", s)
    if m:
        d, mo, y = (int(g) for g in m.groups())
        y += 2000 if y < 100 else 0
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    m = re.search(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{4})", s)
    if m and m.group(1).lower() in _MONTHS:
        return date(int(m.group(2)), _MONTHS[m.group(1).lower()], 1)
    return None


def check_title_blocks(floor) -> dict:
    sheets = []
    issues: list[Issue] = []
    by_layout = {sh.layout: sh for sh in floor.sheets}
    for layout in floor.doc.layouts:
        if layout.name == "Model" or len(layout) == 0:
            continue
        tb = read_title_block(floor.doc, layout)
        sh = by_layout.get(layout.name)
        tb["discipline_from_content"] = sh.discipline if sh else None
        tb["measured_scale"] = (f"1:{sh.scale_ratio(floor.to_m):.0f}" if sh and sh.main else None)
        sheets.append(tb)
    sheets_tb = [s for s in sheets if s["title_block"]]
    for s in sheets:
        if not s["title_block"]:
            issues.append(Issue("warning", "title_block", "no title block found on this sheet", s["layout"]))

    # drawing numbers must be unique per sheet
    nums = defaultdict(list)
    for s in sheets_tb:
        nums[s["fields"].get("drawing_number", "")].append(s["layout"])
    for num, lays in nums.items():
        if len(lays) > 1:
            issues.append(Issue("error", "drawing_number", f"{len(lays)} sheets share drawing number {num or '(empty)'!r}",
                                detail={"sheets": lays}))
    for s in sheets_tb:
        f = s["fields"]
        lay = s["layout"]
        if f.get("job_number") and f.get("drawing_number") and not f["drawing_number"].startswith(f["job_number"]):
            issues.append(Issue("warning", "drawing_number", f"drawing number {f['drawing_number']!r} does not start "
                                f"with the job number {f['job_number']!r}", lay))
        title = f.get("drawing_title", "")
        disc = s.get("discipline_from_content")
        if disc in DISCIPLINE_WORDS and title and not re.search(DISCIPLINE_WORDS[disc], title, re.I):
            issues.append(Issue("error", "drawing_title", f"title {title!r} does not match the sheet's content "
                                f"({DISCIPLINE_NAMES[disc]} layers visible)", lay))
        if title and not re.search("|".join(DISCIPLINE_WORDS.values()), title, re.I):
            issues.append(Issue("warning", "drawing_title", f"title {title!r} names no discipline", lay))
        if not re.search(r"\w", re.sub(r"(?i)^\W+", "", lay)) or re.match(r"(?i)^layout\d*$", lay):
            issues.append(Issue("info", "layout_name", f"layout tab is unnamed ({lay!r})", lay))
        for key in ("drawing_title", "drawing_number", "drawn_by", "checked_by", "designed_by", "date", "scale"):
            if key in f and not f[key]:
                issues.append(Issue("warning", "empty_field", f"{key.replace('_', ' ')} is empty or a placeholder", lay))
        if f.get("scale", "").upper() in ("NST", "NTS") and s.get("measured_scale"):
            issues.append(Issue("info", "scale", f"scale says {f['scale']!r} but the plan viewport plots at "
                                f"{s['measured_scale']}", lay))
        sheet_date = parse_date(f.get("date", ""))
        rev_dates = [(r.get("no", "?"), parse_date(r.get("date", ""))) for r in s["revisions"]]
        known = [(n, d) for n, d in rev_dates if d]
        if sheet_date and known and max(d for _, d in known) > sheet_date.replace(day=28):
            issues.append(Issue("warning", "dates", f"latest revision ({max(d for _, d in known)}) is after the sheet "
                                f"date ({f['date']})", lay))
        if sheet_date and known and all(d < sheet_date for _, d in known) and \
                (sheet_date - max(d for _, d in known)).days > 60:
            issues.append(Issue("warning", "dates", f"sheet dated {f['date']} but its newest revision is dated "
                                f"{max(d for _, d in known):%d-%m-%Y}: revision table not updated?", lay))
        if len(known) >= 2 and len({d for _, d in known}) < len(known):
            issues.append(Issue("warning", "dates", "revisions share the same date: "
                                + ", ".join(f"{n}={d:%d-%m-%Y}" for n, d in known), lay))
        for ht in s["hidden_texts"]:
            issues.append(Issue("warning", "hidden_text", f"zero-height text inside {ht['source']}: {ht['text'][:90]!r}",
                                lay))
        for kind, refs in s.get("grid_refs", {}).items():
            if kind == "numbers":
                vals = sorted({int(v) for v in refs})
                missing = [v for v in range(vals[0], vals[-1] + 1) if v not in vals] if vals else []
            else:
                vals = sorted({ord(v) for v in refs})
                missing = [chr(v) for v in range(vals[0], vals[-1] + 1) if v not in vals] if vals else []
            if missing:
                issues.append(Issue("info", "grid_refs", f"border grid {kind} skip {missing}", lay))
    # fields that should agree across the sheets of one floor
    for key in CONSTANT_FIELDS:
        vals = {s["layout"]: s["fields"].get(key, "") for s in sheets_tb if key in s["fields"]}
        distinct = {v for v in vals.values()}
        if len(distinct) > 1:
            close = [(a, b) for a in distinct for b in distinct if a < b and a and b
                     and difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.85]
            what = "spelled differently" if close else "differs"
            issues.append(Issue("warning", "consistency", f"{key.replace('_', ' ')} {what} between sheets",
                                detail={"values": vals}))
    issues.extend(_foreign_project_text(sheets_tb))
    return {"sheets": sheets, "issues": [asdict(i) for i in issues]}


def _foreign_project_text(sheets) -> list[Issue]:
    """Texts that describe a different project than the sheet's own PROJECT field."""
    out = []
    for s in sheets:
        project_words = set(re.findall(r"[a-z]{3,}", s["fields"].get("project", "").lower()))
        for t in s["hidden_texts"] + s["orphans"]:
            txt = t["text"]
            words = set(re.findall(r"[a-z]{3,}", txt.lower()))
            if re.search(r"(?i)\bproposed\b|\bproject\b|\bbuilding\b", txt) and len(words) >= 3 \
                    and len(words - project_words) > 0.5 * len(words):
                out.append(Issue("error", "foreign_project", f"text from another job in the title block: {txt[:90]!r}",
                                 s["layout"]))
    return out


# ======================================================================= legends
def _legend_entries(floor, items: list[tuple], texts: list[Txt]) -> tuple[list[dict], list[str]]:
    """Pair each legend symbol (by its drawn centre) with the description on its row.

    Returns the rows and the descriptions that have no symbol next to them. Several
    symbol pieces may share one row (the stair-light strip is four inserts)."""
    if not texts or not items:
        return [], []
    h = sorted(t.h for t in texts)[len(texts) // 2] or 0.1
    sym_x = sorted(cx for _, cx, _, _, _ in items)[len(items) // 2]
    column = [t for t in texts if 0 < t.x - sym_x <= 20 * h and re.search(r"[A-Za-z]{3}", t.text)]
    ys = sorted(t.ymid for t in column)
    gaps = [b - a for a, b in zip(ys, ys[1:]) if b - a > 0.5 * h]
    pitch = sorted(gaps)[len(gaps) // 2] if gaps else 3 * h
    rows: dict[tuple, dict] = {}
    paired = set()
    for sym_name, cx, cy, half_w, cls in items:
        best = None
        for j, t in enumerate(column):
            dx, dy = t.x - cx, abs(t.ymid - cy)
            if 0 < dx <= half_w + 16 * h and dy <= 0.55 * pitch and (best is None or (dy, dx) < best[0]):
                best = ((dy, dx), j)
        text = column[best[1]].text if best else None
        if best:
            paired.add(best[1])
        key = (sym_name, text)
        if key not in rows:
            rows[key] = {"block": sym_name, "device_type": cls.device_type, "category": cls.category,
                         "legend_text": text, "catalogue_description": cls.description,
                         "at": [round(cx, 3), round(cy, 3)], "pieces": 0}
        rows[key]["pieces"] += 1
    orphans = [t.text for j, t in enumerate(column) if j not in paired
               and not re.match(r"(?i)^(legend|symbol|description)$", t.text)]
    return list(rows.values()), orphans


def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-z]{3,}", (s or "").lower())) - {"with", "the", "and", "led", "light", "lights", "c/w"}


_KIND_WORDS = {"switch", "sounder", "socket", "detector", "camera", "panel", "point", "unit", "speaker", "pendant",
               "chandelier", "bulkhead", "spot", "strip", "glass", "isolator", "photocell", "sensor", "hood", "wall",
               "down", "track", "intercom", "television", "data", "smoke", "heat", "shave", "cooker", "consumer"}


def _disagree(legend_text: str, description: str) -> bool:
    """True if a legend row and the catalogue clearly describe different things."""
    a, b = _words(legend_text), _words(description)
    if a and b and not (a & b):
        return True
    ka, kb = a & _KIND_WORDS, b & _KIND_WORDS
    return bool(ka and kb and not (ka & kb))


def read_legend(floor, sheet) -> dict:
    """Legend rows of one sheet, from paper space and from secondary viewports onto a model-space legend."""
    cat = floor.catalogue
    layout = floor.doc.paperspace(sheet.layout)
    items, texts, sources = [], [], []
    entries, orphans = [], []
    ps_inserts = [i for i in layout.query("INSERT") if cat.classify(i.dxf.name).known]
    if ps_inserts:
        for ins in ps_inserts:
            g = floor.geometry.get(ins.dxf.name)
            if g is None:
                continue
            (cx, cy), box = drawn_frame(ins, g)
            items.append((ins.dxf.name, cx, cy, (box[2] - box[0]) / 2,
                          cat.classify(ins.dxf.name, {a.dxf.tag: a.dxf.text for a in ins.attribs})))
        texts = [_txt(e, "paper") for e in layout.query("TEXT MTEXT")]
        sources.append("paper space")
        e, o = _legend_entries(floor, items, texts)
        entries += e
        orphans += o
    for vp in sheet.viewports:
        if vp.role == "main" or vp.is_off:
            continue
        frozen = set(vp.frozen_layers)
        items = []
        for s in floor.symbols:
            if not s.on_plan and vp.contains(*s.centre) and s.layer not in frozen and s.known:
                items.append((s.block, s.centre[0], s.centre[1], (s.bbox[2] - s.bbox[0]) / 2,
                              cat.classify(s.block, s.attribs)))
        if not items:
            continue
        texts = [_txt(e, "model") for e in floor.msp.query("TEXT MTEXT")
                 if vp.contains(e.dxf.insert.x, e.dxf.insert.y) and e.dxf.get("layer") not in frozen]
        e, o = _legend_entries(floor, items, texts)
        entries += e
        orphans += o
        sources.append(f"viewport {vp.handle} onto model space")
    return {"sources": sources, "entries": entries, "rows_without_symbol": orphans}


def check_legends(floor) -> dict:
    out, issues = [], []
    for sheet in floor.sheets:
        vp = sheet.main
        if vp is None:
            continue
        legend = read_legend(floor, sheet)
        frozen = set(vp.frozen_layers)
        used = Counter()
        used_blocks = defaultdict(Counter)
        for s in floor.symbols:
            if s.on_plan and vp.contains(*s.centre) and s.layer in floor.layers_on and s.layer not in frozen:
                used[s.device_type] += 1
                used_blocks[s.device_type][s.block] += 1
        entries = legend["entries"]
        legend_types = {e["device_type"] for e in entries}
        legend_blocks = {e["block"] for e in entries}
        missing = {t: n for t, n in used.items() if t not in legend_types}
        unused = sorted(legend_types - set(used))
        other_block = {t: sorted(set(used_blocks[t]) - legend_blocks) for t in used if t in legend_types
                       and set(used_blocks[t]) - legend_blocks}
        texts_by_block = defaultdict(set)
        for e in entries:
            if e["legend_text"]:
                texts_by_block[(e["block"], e["device_type"])].add(e["legend_text"])
        reused = {b: sorted(ts) for (b, _), ts in texts_by_block.items() if len(ts) > 1}
        mismatches = []
        for e in entries:
            if e["legend_text"] is None:
                mismatches.append({**e, "problem": "no description found on the symbol's row"})
                continue
            if _disagree(e["legend_text"], e["catalogue_description"]) and e["block"] not in reused:
                mismatches.append({**e, "problem": "legend text and catalogue disagree"})
        rec = {"sheet": sheet.layout, "discipline": sheet.discipline, "legend_sources": legend["sources"],
               "legend_rows": len(entries), "entries": entries, "rows_without_symbol": legend["rows_without_symbol"],
               "used_on_sheet": dict(used.most_common()),
               "used_not_in_legend": {t: dict(used_blocks[t]) for t in sorted(missing, key=lambda t: -missing[t])},
               "legend_not_used_on_sheet": unused, "block_used_differs_from_legend": other_block,
               "block_on_several_rows": reused, "description_mismatches": mismatches}
        out.append(rec)
        if not entries:
            issues.append(asdict(Issue("error", "legend", "no legend found", sheet.layout)))
            continue
        for t, blocks in rec["used_not_in_legend"].items():
            issues.append(asdict(Issue("warning", "legend", f"{sum(blocks.values())} x {t} on the plan but not in the "
                                       f"legend (blocks {', '.join(blocks)})", sheet.layout)))
        for b, ts in reused.items():
            issues.append(asdict(Issue("warning", "legend", f"block {b!r} stands for {len(ts)} different legend rows: "
                                       + "; ".join(repr(t) for t in ts), sheet.layout)))
        for m in mismatches:
            issues.append(asdict(Issue("warning", "legend", f"{m['block']}: {m['problem']} "
                                       f"(legend {m['legend_text']!r} vs catalogue {m['catalogue_description']!r})",
                                       sheet.layout)))
        group = DISCIPLINE_CATEGORIES.get(sheet.discipline)
        if group:
            foreign = Counter((s.block, s.layer, s.category) for s in floor.symbols
                              if s.on_plan and vp.contains(*s.centre) and s.layer in floor.layers_on
                              and s.layer not in frozen and s.category not in group)
            for (blk, ly, catg), n in foreign.most_common():
                issues.append(asdict(Issue("warning", "layers", f"{n} x {blk} ({catg}) visible on this "
                                           f"{DISCIPLINE_NAMES[sheet.discipline]} sheet because it sits on layer {ly!r}",
                                           sheet.layout)))
        for t, blocks in other_block.items():
            issues.append(asdict(Issue("info", "legend", f"{t}: the plan uses block {', '.join(blocks)} but the legend "
                                       "shows a different block for it", sheet.layout)))
        if legend["rows_without_symbol"]:
            issues.append(asdict(Issue("info", "legend", "legend rows with no symbol block next to them (drawn as "
                                       "linework?): " + "; ".join(repr(t) for t in legend["rows_without_symbol"]),
                                       sheet.layout)))
        if unused:
            issues.append(asdict(Issue("info", "legend", f"{len(unused)} legend rows show symbols not used on this "
                                       f"sheet: {', '.join(unused)}", sheet.layout)))
    return {"sheets": out, "issues": issues}


# ======================================================================= device counts
def device_counts(floor, categories: set[str] | None = None) -> dict:
    rooms = floor.rooms
    rows = []
    for r in rooms.rooms:
        counts = Counter()
        for s in floor.symbols:
            if s.room_id == r.room_id and (categories is None or s.category in categories):
                counts[s.device_type] += 1
        rows.append({"room_id": r.room_id, "name": r.name, "unit": r.unit, "room_type": r.room_type,
                     "area_m2": round(r.area_m2, 1), "status": r.status, "counts": dict(counts.most_common()),
                     "total": sum(counts.values())})
    unassigned = [s for s in floor.symbols if s.on_plan and s.room_id is None
                  and (categories is None or s.category in categories)]
    by_unit = defaultdict(Counter)
    for row in rows:
        by_unit[row["unit"] or "-"].update(row["counts"])
    issues = []
    for row in rows:
        if row["room_type"] in ("bedroom", "lounge", "kitchen", "dining", "cinema", "office") \
                and not any(t.startswith("socket") for t in row["counts"]):
            issues.append(asdict(Issue("warning", "device_counts", f"{row['room_id']} {row['name']} "
                                       f"[{row['unit']}] has no socket outlets")))
    mirror = floor.mirror
    if mirror:
        for p in mirror.pairs:
            a = next(x for x in rows if x["room_id"] == p["a"])
            b = next(x for x in rows if x["room_id"] == p["b"])
            if a["counts"] != b["counts"]:
                diff = {t: [a["counts"].get(t, 0), b["counts"].get(t, 0)]
                        for t in sorted(set(a["counts"]) | set(b["counts"])) if a["counts"].get(t, 0) != b["counts"].get(t, 0)}
                issues.append(asdict(Issue("info", "mirror_consistency", f"mirrored rooms {p['a']} {a['name']} and "
                                           f"{p['b']} {b['name']} have different device counts", detail=diff)))
    return {"rooms": rows, "by_unit": {u: dict(c.most_common()) for u, c in by_unit.items()},
            "unassigned_symbols": [{"handle": s.handle, "block": s.block, "type": s.device_type,
                                    "centre": [round(v, 3) for v in s.centre]} for s in unassigned],
            "issues": issues}


def write_device_counts_csv(result: dict, path) -> str:
    types = sorted({t for row in result["rooms"] for t in row["counts"]})
    # UTF-8 with a byte-order mark: without it Excel reads the file in the ANSI code page and garbles room names
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["room_id", "name", "unit", "room_type", "area_m2", "status", *types, "total"])
        for row in result["rooms"]:
            w.writerow([row["room_id"], row["name"], row["unit"], row["room_type"], row["area_m2"], row["status"],
                        *[row["counts"].get(t, 0) for t in types], row["total"]])
    return str(path)


def run_all_checks(floor) -> dict:
    res = {"floor_id": floor.floor_id, "title_blocks": check_title_blocks(floor), "legends": check_legends(floor),
           "device_counts": device_counts(floor)}
    all_issues = res["title_blocks"]["issues"] + res["legends"]["issues"] + res["device_counts"]["issues"]
    res["summary"] = dict(Counter(i["severity"] for i in all_issues))
    return res


def checks_markdown(res: dict) -> str:
    lines = [f"# Checks for {res['floor_id']}", ""]
    for part in ("title_blocks", "legends", "device_counts"):
        lines.append(f"## {part.replace('_', ' ').title()}")
        lines.append("")
        iss = res[part]["issues"]
        if not iss:
            lines.append("No issues.")
        for i in sorted(iss, key=lambda i: ["error", "warning", "info"].index(i["severity"])):
            where = f" ({i['sheet']})" if i.get("sheet") else ""
            lines.append(f"- **{i['severity']}**{where}: {i['message']}")
            if i.get("detail"):
                lines.append(f"  - detail: `{json.dumps(i['detail'], default=str)[:300]}`")
        lines.append("")
    return "\n".join(lines)
