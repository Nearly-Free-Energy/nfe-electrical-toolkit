"""Integration test on the pilot floor (F6). Skipped unless the drawing is available:
put a copy (one .dwg) in work/input/F6/ or set NFE_F6_DWG to its path. The drawing is
client data and never goes into the repository."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
F6 = Path(os.environ.get("NFE_F6_DWG") or next(iter(sorted((ROOT / "work" / "input" / "F6").glob("*.dwg"))), ROOT / "missing.dwg"))

pytestmark = pytest.mark.skipif(not F6.is_file(), reason="F6 drawing not available")


@pytest.fixture(scope="module")
def floor(tmp_path_factory):
    from nfe_electrical.floor import Floor
    return Floor.open(F6, tmp_path_factory.mktemp("f6work"), floor_id="F6")


def test_normaliser(floor):
    assert floor.units.detected == "m"
    assert floor.base.plotted == "PDF2"
    assert floor.block_audit.far_blocks >= 20
    assert len(floor.symbols_on_plan()) > 350
    assert {sh.discipline for sh in floor.sheets} >= {"lighting", "small_power", "ict_fire"}


def test_rooms_and_units(floor):
    s = floor.rooms.summary()
    assert s["labels_bounded"] >= 28 and s["rooms_suspect"] == 0
    assert floor.mirror is not None and set(floor.mirror.sides) == {"W", "E"}
    bed = floor.rooms.get("Bedroom [W]")
    assert 15 < bed.area_m2 < 25


def test_checks_find_known_problems(floor):
    from nfe_electrical.checks import check_title_blocks
    res = check_title_blocks(floor)
    msgs = " ".join(i["message"] for i in res["issues"])
    assert "share drawing number" in msgs
    assert "another job" in msgs


def test_rules_regenerate_bedside_pairs(floor):
    from nfe_electrical.compare import compare_devices
    from nfe_electrical.overlay import Overlay
    from nfe_electrical.rules import apply_rules, load_rules
    ov = Overlay.open(floor, "pytest_rules", fresh=True)
    rules = load_rules(ROOT / "rules" / "bs7671" / "apartment_type_a_small_power.yaml", floor.catalogue)
    apply_rules(floor, ov, rules)
    beds = [r.room_id for r in floor.rooms.rooms if r.room_type == "bedroom"]
    cmp = compare_devices(floor, ov, beds)
    assert cmp["totals"]["pairs_within_0.5m"] == 4
    assert ov.validate(libredwg=False)["only_nfe_layers"]
