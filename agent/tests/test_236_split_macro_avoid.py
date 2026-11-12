"""§236 (run 20261111_140924, 4_cts S -324 on u_ucut_mn.u_r/g_egress.mr_q):
JANUS_PIN_FENCE_SPLIT put mr_q at the straight-line point (872.9,407.3),
which is INSIDE u_user (40.1,80.2)-(1320.4,1360.5). The split target is now
the f*L point along a Manhattan route, pushed out of every macro (+halo).
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PF = ROOT / "asic_flow/scripts/hooks/janus_pin_fence.tcl"
PROCS = ("split_xy", "in_rect", "push_out", "split_pick")
UUSER = "{40.1 80.2 1320.4 1360.5}"
CORE = "{10 10 2450 1430}"


def _tcl(body):
    if not shutil.which("tclsh"):
        pytest.skip("tclsh missing")
    src = PF.read_text()
    got = re.findall(rf"(?ms)^proc janus_pf::(?:{'|'.join(PROCS)}) .*?^\}}", src)
    assert len(got) == len(PROCS)
    r = subprocess.run(["tclsh"], input="namespace eval janus_pf {}\n" + "\n".join(got) + "\n" + body,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.split()


def test_run_140924_target_was_inside_u_user():
    out = _tcl(f"lassign [janus_pf::split_xy 50.9 80.2 1422.1 625.5 0.4] x y\n"
               f"puts [format %.1f $x]; puts [format %.1f $y]\n"
               f"puts [janus_pf::in_rect $x $y {UUSER} 0]")
    # log printed (872.9,407.3) = this point after clamp's half-cell offset
    assert out == ["873.6", "407.4", "1"]


def test_run_140924_pick_goes_round_the_south_channel():
    x, y, c, tag = _tcl(f"puts [janus_pf::split_pick 50.9 80.2 1422.1 625.5 0.4 [list {UUSER}] 2.0 {CORE}]")
    x, y, c = float(x), float(y), float(c)
    assert tag == "vfirst"
    assert y < 80.2 and 1150 < x < 1250       # south channel, ~40% from Q
    assert c < 10                              # ~no detour
    assert _tcl(f"puts [janus_pf::in_rect {x} {y} {UUSER} 0]") == ["0"]


def test_straight_kept_when_macro_free():
    x, y, c, tag = _tcl("puts [janus_pf::split_pick 100 1000 1100 500 0.4 {{2000 0 2100 100}} 2 {}]")
    assert tag == "straight" and float(x) == pytest.approx(700) and float(y) == pytest.approx(700)
    assert float(c) == pytest.approx(0)


def test_push_out_respects_core_and_abutted_macros():
    # pushed left out of A lands in abutted B -> pushed again, ends outside both
    x, y = _tcl("puts [janus_pf::push_out 105 50 {{100 0 200 100} {0 0 100 100}} 1 {-50 -50 300 300}]")
    assert _tcl(f"puts [janus_pf::in_rect {x} {y} {{100 0 200 100}} 1]") == ["0"]
    assert _tcl(f"puts [janus_pf::in_rect {x} {y} {{0 0 100 100}} 1]") == ["0"]
    # macro fills the core -> no legal point
    assert _tcl("puts [llength [janus_pf::push_out 50 50 {{0 0 100 100}} 1 {0 0 100 100}]]") == ["0"]


def test_split_one_uses_pick_and_detour_guard():
    src = PF.read_text()
    body = re.search(r"(?ms)^proc janus_pf::split_one .*?^\}", src).group(0)
    assert "janus_pf::split_pick" in body and "split_xy" not in body
    assert "JANUS_PIN_FENCE_SPLIT_MAX_DETOUR_UM" in body and "JANUS_PIN_FENCE_SPLIT_HALO_UM" in body
    assert "isBlock" in body
