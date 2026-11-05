"""§209 (run 20261104_181842, main f206f56 = §208): 3_5 detail place died with
DPL-0036, 2226 cells unplaced (961 of the 1000 listed in u_ucut_mn, rest top
glue / u_ucut_m / u_clkgen). The §208 post-GPL region bind put 5288 glob members
(only 749 anchored) into a 40 um strip whose inner 20 um is MACRO_BLOCKAGE_HALO,
and DPL fences are exclusive, so GPL glue/rsz buffers in the strip were trapped.
msip_q found no u_user pins because ASAP7 flops only have QN (inverter between
reg and pin). Fix: no post-GPL region by default, strip offset by the blockage
halo, pin discovery follows one INV/BUF hop; DPL-0036 classifier.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PF = ROOT / "asic_flow/scripts/hooks/janus_pin_fence.tcl"
MK = (ROOT / "asic_flow/configs/janus_soc_top.mk").read_text()
FIXD = Path(__file__).resolve().parent / "fixtures" / "t198d"
OR = os.environ.get("JANUS_TEST_OPENROAD", "/home/ubuntu/dk/bin/openroad")
PLAT = os.environ.get("JANUS_TEST_ASAP7",
                      "/home/ubuntu/dk/rootfs/OpenROAD-flow-scripts/flow/platforms/asap7")


def _need_or():
    if not (os.access(OR, os.X_OK) and os.path.isdir(PLAT)):
        pytest.skip("openroad / asap7 platform not available")


def _run(tmp_path, tag, body, env_extra, hop=False):
    _need_or()
    d = tmp_path / tag
    shutil.copytree(FIXD, d)
    if hop:   # rx2 <- BUF <- u_m2/dout (reg not directly on the macro pin)
        v = (d / "t.v").read_text()
        v = v.replace(".D(mo2), .QN(x2));",
                      ".D(mo2b), .QN(x2));\nBUFx2_ASAP7_75t_R ub2 (.A(mo2), .Y(mo2b));")
        (d / "t.v").write_text(v)
    base = (d / "t.tcl").read_text().split("global_placement")[0]
    (d / "pf.tcl").write_text(base + body)
    env = dict(os.environ, T198_PLATFORM=PLAT, **env_extra)
    r = subprocess.run([OR, "-no_init", "-exit", "pf.tcl"], cwd=d, env=env,
                       capture_output=True, text=True, timeout=180)
    return r.returncode, r.stdout + r.stderr


FLOW = f"""
source {PF}
global_placement -skip_io -density 0.4
puts "T209 regions [llength [$b getRegions]] wrapped [llength [info commands ::janus_pf::gpl_orig]]"
detailed_placement
check_placement
foreach n {{rx rx2}} {{ set i [$b findInst $n]; lassign [$i getLocation] x y
  puts [format "T209 dp %s %.1f %.1f %s" $n [expr {{$x/1000.0}}] [expr {{$y/1000.0}}] [$i getPlacementStatus]] }}
"""


def _xy(out, n):
    m = re.search(rf"T209 dp {n} ([\d.]+) ([\d.]+) PLACED", out)
    assert m, out[-3000:]
    return float(m.group(1)), float(m.group(2))


def test_default_no_post_gpl_region_anchor_legalized_at_pins(tmp_path):
    rc, out = _run(tmp_path, "def", FLOW, {"JANUS_PIN_FENCE": "fx:u_m:rx",
                                          "JANUS_PIN_FENCE_MODE": "prelock"})
    assert rc == 0, out[-3000:]
    assert "fx members 1 area" in out and "prelocked 1 (mode prelock)" in out
    assert "fx released 1 prelocked member(s)" in out
    assert "no post-GPL region (post_bind none)" in out
    assert "bound members" not in out
    assert "T209 regions 0 wrapped 0" in out, out[-3000:]
    x, y = _xy(out, "rx")
    # strip (190.0,222.1)-(230.0,262.1); DPL only legalizes locally
    assert 185.0 <= x <= 232.0 and 218.0 <= y <= 266.0, (x, y)


def test_post_bind_locked_binds_only_anchors(tmp_path):
    rc, out = _run(tmp_path, "lk", FLOW, {"JANUS_PIN_FENCE": "fx:u_m:rx r1",
                                         "JANUS_PIN_FENCE_POST_BIND": "locked"})
    assert rc == 0, out[-3000:]
    assert re.search(r"fx bound members (\d+)/\1", out), out[-3000:]
    assert "T209 regions 1" in out


def test_gap_from_macro_blockage_halo(tmp_path):
    rc, out = _run(tmp_path, "gap", FLOW, {"JANUS_PIN_FENCE": "fx:u_m:rx",
                                          "MACRO_BLOCKAGE_HALO": "10"})
    assert rc == 0, out[-3000:]
    # u_m W edge at x=230: strip now 10 um off the edge
    assert "fx edge W pins 2 strip (180.0,222.1)-(220.0,266.1) um" in out, out[-3000:]
    x, _ = _xy(out, "rx")
    assert x <= 222.0, x


def test_pin_discovery_follows_one_buffer_hop(tmp_path):
    env = {"JANUS_PIN_FENCE": "fy:u_m2:rx2"}
    rc, out = _run(tmp_path, "hop", FLOW, env, hop=True)
    assert rc == 0, out[-3000:]
    assert "no u_m2 pins on member nets" not in out, out[-3000:]
    assert "fy members 1 area" in out and "prelocked 1 (mode prelock)" in out, out[-3000:]


def test_dpl_fence_is_exclusive_for_non_members(tmp_path):
    """Evidence for the root cause: non-members inside a bound INCLUSIVE region
    are pushed out by DPL (fence semantics), members far away are pulled in."""
    body = """
global_placement -skip_io -density 0.4
set reg [odb::dbRegion_create $b fx]; $reg setRegionType INCLUSIVE
odb::dbBox_create $reg 100000 100000 140000 140000
set g [odb::dbGroup_create $b fx_grp]; $reg addGroup $g
$g addInst [$b findInst r5]
[$b findInst r5] setLocation 270000 20000
[$b findInst r6] setLocation 120000 120000
detailed_placement
foreach n {r5 r6} { lassign [[$b findInst $n] getLocation] x y; puts [format "EX %s %.1f %.1f" $n [expr {$x/1000.0}] [expr {$y/1000.0}]] }
"""
    rc, out = _run(tmp_path, "ex", body, {})
    assert rc == 0, out[-3000:]
    r5 = [float(v) for v in re.search(r"EX r5 ([\d.]+) ([\d.]+)", out).groups()]
    r6 = [float(v) for v in re.search(r"EX r6 ([\d.]+) ([\d.]+)", out).groups()]
    assert 100 <= r5[0] <= 140 and 100 <= r5[1] <= 140, r5
    assert not (100 <= r6[0] < 140 and 100 <= r6[1] < 140), r6


def test_mk_post_bind_none():
    m = re.search(r"^export JANUS_PIN_FENCE_POST_BIND\s*=\s*(\S+)", MK, re.M)
    assert m and m.group(1) == "none"


def test_triage_dpl_0036():
    sys.path.insert(0, str(ROOT / "asic_flow/utils"))
    import report_bundle as rb
    log = ("[INFO DPL-0035]  u_ucut_mn/_4462_\n"
           "[ERROR DPL-0036] Detailed placement failed inside DPL.\n"
           "Error: detail_place.tcl, 40 DPL-0036\n")
    t = rb.triage_failure([("3_5_place_dp.log", log)], "place")
    assert t["category"] == "dpl-legalize", t
    assert t["code"] == "DPL-0036"
