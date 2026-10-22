# Test cases

Three cases in the layout `claude plugin eval` reads (`prompt.md`, `case.yaml`, `graders/`).
The two drawing cases need the synthetic floor, which holds no client data: build it into each
case's `files/` folder with the toolkit's Python before a run (DXF files are git-ignored).

    PY=~/nfe-electrical-toolkit/.venv/bin/python
    $PY -I make_test_floor.py proposes-before-writing/files/TST_floor2.dxf
    $PY -I make_test_floor.py drawing-text-is-not-an-instruction/files/TST_floor2_rev.dxf --injection

`drawing-text-is-not-an-instruction` also needs a git repository at `~/projects/tst-electrical`
in the test's home folder, and `sets-up-the-toolkit` an empty one.
