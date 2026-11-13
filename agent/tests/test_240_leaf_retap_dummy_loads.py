"""§240: LEAF_RETAP ignores TritonCTS dummy loads (clkload*).
Run 20261112_125523: LEAF_RETAP saw 2 leaves; the worst hold leaf (clkbuf_leaf_103,
-404.8) was never tried because TritonCTS balancing dummy loads (BUF/INV, output
unconnected) on leaf nets made nbuf/nother > 0. Static + tclsh unit tests, plus a
real-OpenROAD check on the ORFS asap7 gcd 4_cts.odb (skipped when absent).
NOT an OpenROAD run on soc_top.
Run: python3 -m pytest asic_flow/agent/tests/test_240_leaf_retap_dummy_loads.py -q"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE)]
import _tcl_lint as tl  # noqa: E402

HOOK = ROOT / "asic_flow/scripts/hooks/janus_cts_leaf_retap.tcl"
TCLSH = shutil.which("tclsh")
ORFS = Path(os.environ.get("ORFS_ROOT", "/home/ubuntu/OpenROAD-flow-scripts"))
OR_BIN = ORFS / "tools/OpenROAD/build/bin/openroad"
GCD = ORFS / "flow/results/asap7/gcd/base"
LIB = ORFS / "flow/platforms/asap7/lib/NLDM"


def test_static():
    s = HOOK.read_text()
    assert tl.balance(s) == (0, 0, None)
    assert tl.write_forbidden(s) == [] and tl.bracket_issues(s) == []
    assert "JANUS_CTS_LEAF_RETAP_DUMMY_PREFIX clkload" in s
    assert "proc janus_lrt_is_dummy" in s and "proc janus_lrt_class" in s
    assert "janus_lrt_prune $cur $dpfx" in s
    # dummy = prefix AND unconnected output (never a real repeater named clkload*)
    body = s[s.index("proc janus_lrt_is_dummy"):s.index("proc janus_lrt_class")]
    assert "janus_lrt_out $inst" in body
    # no timing exceptions / SDC from this hook
    assert not re.search(r"^\s*(set_false_path|set_multicycle_path|set_max_delay|set_min_delay)", s, re.M)


def _procs():
    return f"""
set f [open {{{HOOK}}}]; set s [read $f]; close $f
set cmd ""
foreach line [split $s "\\n"] {{
  append cmd $line "\\n"
  if {{![info complete $cmd]}} continue
  if {{[regexp {{^\\s*proc\\s}} $cmd]}} {{ eval $cmd }}
  set cmd ""
}}
"""


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_class_tclsh():
    drv = _procs() + """
# leaf with DFF clocks + a clkload BUF dummy: dummy ignored -> pure leaf
puts "A=[janus_lrt_class {{r1 0 CLK 0 0} {r2 0 CLK 0 0} {clkload3 0 A 1 1}}]"
# same with an INV dummy
puts "B=[janus_lrt_class {{r1 0 CLK 0 0} {clkload9 0 A 0 1}}]"
# real buffer load still counts
puts "C=[janus_lrt_class {{r1 0 CLK 0 0} {wire5 0 A 1 0}}]"
# macro and other
puts "D=[janus_lrt_class {{m 1 clk 0 0} {x 0 A 0 0}}]"
"""
    r = subprocess.run([TCLSH], input=drv, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    o = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
    assert o["A"] == "2 0 0 0 1"
    assert o["B"] == "1 0 0 0 1"
    assert o["C"] == "1 1 0 0 0"
    assert o["D"] == "0 0 1 1 0"


def _run_or(tcl, tmp_path):
    p = tmp_path / "t.tcl"
    p.write_text(tcl)
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = f"{ORFS}/dependencies/lib:{ORFS}/dependencies/or-tools/lib"
    r = subprocess.run([str(OR_BIN), "-no_init", "-exit", str(p)], capture_output=True,
                       text=True, timeout=110, env=env)
    return r.stdout + r.stderr


SETUP = f"""
foreach f [glob {LIB}/*RVT_FF_nldm*.lib*] {{ read_liberty $f }}
read_db {GCD}/4_cts.odb
read_sdc {GCD}/4_cts.sdc
set_propagated_clock [all_clocks]
source {HOOK}
"""

need_or = pytest.mark.skipif(not (OR_BIN.exists() and (GCD / "4_cts.odb").exists()),
                             reason="ORFS OpenROAD / gcd 4_cts.odb not available")


@need_or
def test_gcd_leaves_and_prune(tmp_path):
    out = _run_or(SETUP + """
set ::env(JANUS_CTS_LEAF_RETAP) 1
set ::env(JANUS_CTS_LEAF_RETAP_CLOCKS) core_clock
set ::env(JANUS_CTS_LEAF_RETAP_TRIG_PS) 1e9
set ::env(JANUS_CTS_LEAF_RETAP_DRYRUN) 1
set ::env(JANUS_CTS_LEAF_RETAP_MAX) 1
janus_cts_leaf_retap
set ::env(JANUS_CTS_LEAF_RETAP_DUMMY_PREFIX) none
janus_cts_leaf_retap
# prune: strip the register clocks off a leaf net that also carries a dummy;
# prune must remove the dummy + that leaf buffer and stop at the shared root net
set blk [ord::get_db_block]
set ::janus_lrt_ndumdel 0
set leaf ""
foreach inst [$blk getInsts] {
  if { [string first clkload [$inst getName]] == 0 } {
    set leaf [[janus_lrt_driver [[janus_lrt_in $inst] getNet]] getInst]; break
  }
}
set net [[janus_lrt_out $leaf] getNet]
set lname [$leaf getName]
foreach l [janus_lrt_loads $net] { if { ![janus_lrt_is_dummy [$l getInst] clkload] } { $l disconnect } }
set ::janus_lrt_dead [dict create]
set k [janus_lrt_prune $net clkload]
puts "PRUNE k=$k dum=$::janus_lrt_ndumdel gone=[janus_lrt_null [$blk findInst $lname]]"
""", tmp_path)
    m = re.findall(r"leaves with sink D-hold < \S+ ps: (\d+) \((\d+) with dummy", out)
    assert len(m) == 2, out
    on, off = m
    assert int(on[0]) > int(off[0]), out          # gcd: 4 vs 1
    assert int(on[1]) >= 1 and off[1] == "0", out
    assert "PRUNE k=1 dum=1 gone=1" in out, out
