"""Work out the real model units of a drawing instead of trusting $INSUNITS.

Two of the pilot project's files declare metres but are drawn in millimetres,
so every signal here comes from geometry: the plan viewport's plot scale, the
drawn size of electrical symbols, room-label text heights and the plan size.
Fewer than three signals never overrule a declared mm, cm or m: when the plan
shows no symbols or labels, the two that remain come from the same viewport.
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field

INSUNITS = {0: "unitless", 1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m"}
INSUNITS_CODE = {"mm": 4, "cm": 5, "m": 6}
TO_M = {"mm": 0.001, "cm": 0.01, "m": 1.0}

# plausible real-world ranges, in metres (plot scale as 1:N)
_RANGES = {
    "plot_scale_1_to_n": (10.0, 1000.0),
    "symbol_size_m": (0.05, 2.0),
    "label_height_m": (0.04, 1.0),
    "plan_extent_m": (3.0, 600.0),
}
MIN_SIGNALS_TO_OVERRULE = 3


@dataclass
class UnitsReport:
    declared_code: int
    declared: str
    detected: str
    to_m: float
    declared_ok: bool
    confidence: float
    signals: list[dict] = field(default_factory=list)
    note: str = ""

    @property
    def insunits(self) -> int:
        return INSUNITS_CODE[self.detected]

    def as_dict(self) -> dict:
        return asdict(self)


def detect_units(declared_code: int, plan_scale: float | None, paper_unit_mm: float | None,
                 symbol_sizes: list[float], label_heights: list[float],
                 plan_window: tuple[float, float, float, float] | None) -> UnitsReport:
    """Vote between mm, cm and m. Each signal votes for every unit that makes its value plausible."""
    signals = []

    def add(name: str, raw: float | None, convert):
        if raw is None or raw <= 0:
            return
        lo, hi = _RANGES[name]
        votes = [u for u, f in TO_M.items() if lo <= convert(raw, f) <= hi]
        signals.append({"signal": name, "raw_value": round(raw, 6), "plausible_units": votes})

    if plan_scale and paper_unit_mm:
        add("plot_scale_1_to_n", plan_scale, lambda v, f: v * f * 1000.0 / paper_unit_mm)
    if symbol_sizes:
        add("symbol_size_m", statistics.median(symbol_sizes), lambda v, f: v * f)
    if label_heights:
        add("label_height_m", statistics.median(label_heights), lambda v, f: v * f)
    if plan_window:
        add("plan_extent_m", max(plan_window[2] - plan_window[0], plan_window[3] - plan_window[1]), lambda v, f: v * f)

    declared = INSUNITS.get(declared_code, f"code {declared_code}")
    tally = {u: sum(u in s["plausible_units"] for s in signals) for u in TO_M}
    best = max(tally.values()) if tally else 0
    tied = [u for u, n in tally.items() if n == best]
    detected = declared if declared in tied else ("m" if "m" in tied else tied[0])
    note = ""
    if declared in TO_M and detected != declared and len(signals) < MIN_SIGNALS_TO_OVERRULE:
        note = (f"only {len(signals)} geometry signal(s), voting for {detected}: too few to overrule "
                f"the declared {declared}")
        detected = declared
    confidence = round(tally[detected] / len(signals), 2) if signals else 0.0
    return UnitsReport(declared_code=declared_code, declared=declared, detected=detected, to_m=TO_M[detected],
                       declared_ok=(declared == detected), confidence=confidence, signals=signals, note=note)
